"""Offline authentication boundary tests; no SDK, database or network required."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class OperationalError(Exception):
    pass


class DatabaseAuth(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        folder = Path(self.directory.name)
        self.ca = folder / 'ca.pem'
        self.token_file = folder / 'projected-token'
        self.ca.write_text('PUBLIC CA TEST FIXTURE')
        self.token_file.write_text('PROJECTED TOKEN TEST FIXTURE')
        self.psycopg = types.ModuleType('psycopg')
        self.psycopg.connect = MagicMock()
        self.psycopg.OperationalError = OperationalError
        rows = types.ModuleType('psycopg.rows')
        rows.dict_row = object()
        self.credential = MagicMock()
        self.credential.get_token.return_value = types.SimpleNamespace(token='FAKE_ACCESS_TOKEN')
        self.identity = types.ModuleType('azure.identity')
        self.identity.WorkloadIdentityCredential = MagicMock(return_value=self.credential)
        modules = patch.dict('sys.modules', {
            'psycopg': self.psycopg, 'psycopg.rows': rows,
            'azure': types.ModuleType('azure'), 'azure.identity': self.identity,
        })
        modules.start()
        self.addCleanup(modules.stop)
        settings = patch.dict(os.environ, {
            'DATABASE_AUTH_MODE': 'workload_identity', 'PGHOST': 'private.example.test',
            'PGDATABASE': 'trafficxx', 'PGUSER': 'trafficxx-lab-browse',
            'PGSSLROOTCERT': str(self.ca), 'AZURE_TENANT_ID': 'tenant',
            'AZURE_CLIENT_ID': 'client', 'AZURE_FEDERATED_TOKEN_FILE': str(self.token_file),
        }, clear=True)
        settings.start()
        self.addCleanup(settings.stop)
        spec = importlib.util.spec_from_file_location('isolated_db_auth', ROOT / 'trafficxx/db.py')
        self.db = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.db)
        cleanup = patch.object(self.db.atexit, 'register')
        self.register = cleanup.start()
        self.addCleanup(cleanup.stop)

    def test_default_local_url_behavior_is_preserved_without_sdk(self):
        os.environ.pop('DATABASE_AUTH_MODE')
        os.environ['DATABASE_URL'] = 'postgresql://local/test'
        self.db.connect()
        self.assertEqual(self.psycopg.connect.call_args.args, ('postgresql://local/test',))
        self.identity.WorkloadIdentityCredential.assert_not_called()

    def test_cloud_uses_only_explicit_workload_credential_and_verified_tls(self):
        os.environ['PGSSLMODE'] = 'disable'
        os.environ['PGPASSWORD'] = 'FAKE_ENV_PASSWORD_MUST_NOT_BE_USED'
        self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_called_once_with(
            tenant_id='tenant', client_id='client', token_file_path=str(self.token_file))
        self.credential.get_token.assert_called_once_with(
            'https://ossrdbms-aad.database.windows.net/.default')
        args = self.psycopg.connect.call_args.kwargs
        self.assertEqual(args['password'], 'FAKE_ACCESS_TOKEN')
        self.assertEqual(args['sslmode'], 'verify-full')
        self.assertEqual(args['sslrootcert'], str(self.ca))
        self.assertEqual(args['user'], 'trafficxx-lab-browse')
        self.assertEqual(args['port'], 5432)
        self.register.assert_called_once_with(self.credential.close)

    def test_sdk_credential_is_reused_but_token_requested_per_connection(self):
        self.credential.get_token.side_effect = [
            types.SimpleNamespace(token='FIRST'), types.SimpleNamespace(token='REFRESHED')]
        self.db.connect()
        self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_called_once()
        self.assertEqual(self.credential.get_token.call_count, 2)
        self.assertEqual([call.kwargs['password'] for call in self.psycopg.connect.call_args_list],
                         ['FIRST', 'REFRESHED'])

    def test_concurrent_connections_initialize_one_credential(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.db.connect(), range(16)))
        self.identity.WorkloadIdentityCredential.assert_called_once()
        self.assertEqual(self.credential.get_token.call_count, 16)

    def test_pool_parameter_callback_resolves_new_token_without_connecting(self):
        self.credential.get_token.side_effect = [
            types.SimpleNamespace(token='FIRST'), types.SimpleNamespace(token='REFRESHED')]
        first, second = self.db.connection_parameters(), self.db.connection_parameters()
        self.assertEqual((first['password'], second['password']), ('FIRST', 'REFRESHED'))
        self.assertEqual(second['sslmode'], 'verify-full')
        self.psycopg.connect.assert_not_called()

    def test_auth_failure_is_redacted_and_never_falls_back_to_database(self):
        self.credential.get_token.side_effect = RuntimeError('FAKE_SECRET_IN_RAW_ERROR')
        with self.assertRaises(OperationalError) as result:
            self.db.connect()
        self.assertNotIn('FAKE_SECRET', str(result.exception))
        self.assertTrue(result.exception.__suppress_context__)
        self.psycopg.connect.assert_not_called()

    def test_empty_sdk_token_is_rejected(self):
        self.credential.get_token.return_value = types.SimpleNamespace(token='')
        with self.assertRaises(OperationalError):
            self.db.connect()
        self.psycopg.connect.assert_not_called()

    def test_conflicting_url_is_rejected_in_cloud_mode(self):
        os.environ['DATABASE_URL'] = 'postgresql://unintended-human-login/test'
        with self.assertRaises(OperationalError):
            self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_not_called()
        self.psycopg.connect.assert_not_called()

    def test_unknown_mode_is_rejected_without_fallback(self):
        os.environ['DATABASE_AUTH_MODE'] = 'typo'
        with self.assertRaises(OperationalError):
            self.db.connect()
        self.psycopg.connect.assert_not_called()

    def test_missing_required_settings_fail_before_token_request(self):
        for name in ('PGHOST', 'PGDATABASE', 'PGUSER', 'AZURE_CLIENT_ID',
                     'AZURE_TENANT_ID', 'AZURE_FEDERATED_TOKEN_FILE'):
            with self.subTest(name=name):
                value = os.environ.pop(name)
                try:
                    with self.assertRaises(OperationalError):
                        self.db.connect()
                finally:
                    os.environ[name] = value
        self.identity.WorkloadIdentityCredential.assert_not_called()

    def test_missing_ca_or_projected_file_fails_before_token_request(self):
        for name in ('PGSSLROOTCERT', 'AZURE_FEDERATED_TOKEN_FILE'):
            with self.subTest(name=name), patch.dict(os.environ, {name: str(self.ca.parent / 'missing')}):
                with self.assertRaises(OperationalError):
                    self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_not_called()

    def test_invalid_port_fails_before_token_request(self):
        for port in ('no', '0', '-1', '65536'):
            with self.subTest(port=port), patch.dict(os.environ, {'PGPORT': port}):
                with self.assertRaises(OperationalError):
                    self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_not_called()

    def test_identity_configuration_change_requires_process_restart(self):
        self.db.connect()
        os.environ['AZURE_CLIENT_ID'] = 'another-client'
        with self.assertRaises(OperationalError):
            self.db.connect()
        self.identity.WorkloadIdentityCredential.assert_called_once()
        self.assertEqual(self.psycopg.connect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
