"""Offline control-flow/rollback tests; actual SQL permissions need Azure."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('operations_permissions', ROOT / 'scripts/check-operations-permissions.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SqlError(Exception):
    sqlstate = '42501'


class Cursor:
    def __init__(self, row=None, rowcount=1):
        self.row, self.rowcount = row, rowcount

    def fetchall(self):
        return self.row or []

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.statements = []
        self.dirty = self.committed = False
        self.rollbacks = 0
        self.allow_forbidden = False
        self.error_state = '42501'
        self.fail_aggregate = False
        self.bad_metric = False
        self.workers = [dict(name='publisher', last_error='DO_NOT_LOG_RAW', age_seconds=1.5)]

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if error_type:
            self.rollback()
        else:
            self.committed = self.dirty

    def rollback(self):
        self.rollbacks += 1
        self.dirty = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql in module.DENIED.values():
            if self.allow_forbidden:
                self.dirty = True
                return Cursor()
            error = SqlError()
            error.sqlstate = self.error_state
            raise error
        if sql.startswith(('SAVEPOINT', 'ROLLBACK TO', 'RELEASE', 'SET TRANSACTION')):
            return Cursor()
        if 'FROM pg_stat_ssl' in sql:
            return Cursor(dict(database='trafficxx', identity=module.ROLE, ssl=True, version='TLSv1.3',
                               rolsuper=False, rolcreatedb=False, rolcreaterole=False, rolreplication=False,
                               rolbypassrls=False, admin=False))
        if sql == 'SELECT 1 AS alive':
            return Cursor({'alive': 1})
        if sql == module.BACKLOG_SQL:
            if self.fail_aggregate:
                raise SqlError()
            return Cursor(dict(outbox_pending=0, outbox_oldest_seconds=-1 if self.bad_metric else 0,
                               processing_outstanding=0, processing_oldest_seconds=0, publish_failures_total=0))
        if sql == module.COUNTS_SQL:
            return Cursor(dict(processed_total=16, processing_failures_total=0, observed_dead_letters_total=1))
        if sql == module.HEARTBEATS_SQL:
            return Cursor(self.workers)
        raise AssertionError(sql)


class OperationsPermissions(unittest.TestCase):
    def run_with_sdk(self, connection, wrong_client=False):
        credential = MagicMock()
        credential.__enter__.return_value = credential
        credential.get_token.return_value = types.SimpleNamespace(token='FAKE_TOKEN_NEVER_PRINT')
        identity = types.ModuleType('azure.identity')
        identity.WorkloadIdentityCredential = MagicMock(return_value=credential)
        psycopg = types.ModuleType('psycopg')
        psycopg.connect = MagicMock(return_value=connection)
        rows = types.ModuleType('psycopg.rows')
        rows.dict_row = object()
        claims = base64.urlsafe_b64encode(json.dumps({'sub': 'system:serviceaccount:trafficxx:operations'}).encode()).decode().rstrip('=')
        path = MagicMock()
        path.read_text.return_value = 'header.' + claims + '.signature'
        path.is_file.return_value = True
        env = dict(AZURE_CLIENT_ID='wrong' if wrong_client else module.CLIENT,
                   AZURE_TENANT_ID=module.TENANT, AZURE_FEDERATED_TOKEN_FILE='/fake/projection')
        output, error = io.StringIO(), None
        with patch.dict('sys.modules', {'azure': types.ModuleType('azure'), 'azure.identity': identity,
                                       'psycopg': psycopg, 'psycopg.rows': rows}), \
                patch.dict(os.environ, env, clear=True), patch.object(module, 'Path', return_value=path), \
                contextlib.redirect_stdout(output):
            try:
                module.run()
            except Exception as caught:
                error = caught
        return output.getvalue(), error, identity, psycopg

    def test_four_positive_paths_and_thirty_denials(self):
        conn = Connection()
        result = module.verify_permissions(conn)
        self.assertEqual(len(result['allowed']), 4)
        self.assertEqual(len(result['denied']), 30)
        self.assertEqual(result['snapshot']['metrics']['observed_dead_letters_total'], 1)
        self.assertFalse(result['snapshot']['native_queue_counts_available'])
        self.assertEqual(result['broker_messages_received'], 0)
        self.assertFalse(conn.dirty)

    def test_every_unexpected_permission_success_is_rejected(self):
        for name, sql in module.DENIED.items():
            with self.subTest(name=name):
                conn = Connection()
                conn.allow_forbidden = True
                with self.assertRaises(ValueError):
                    module.expect_denied(conn, name, sql)
                self.assertEqual(conn.statements[-2:], ['ROLLBACK TO SAVEPOINT permission_probe',
                                                       'RELEASE SAVEPOINT permission_probe'])

    def test_wrong_sqlstate_does_not_count_as_permission_denial(self):
        conn = Connection()
        conn.error_state = '23503'
        with self.assertRaises(SqlError):
            module.expect_denied(conn, 'outbox_insert', module.DENIED['outbox_insert'])

    def test_negative_or_nonfinite_metrics_are_rejected(self):
        conn = Connection()
        conn.bad_metric = True
        with self.assertRaises(ValueError):
            module.positive_checks(conn)
        for value in (-1, float('nan'), float('inf')):
            conn = Connection()
            conn.workers[0]['age_seconds'] = value
            with self.assertRaises(ValueError):
                module.positive_checks(conn)

    def test_raw_worker_errors_are_redacted(self):
        result = module.verify_permissions(Connection())
        self.assertNotIn('DO_NOT_LOG_RAW', json.dumps(result))
        self.assertTrue(result['snapshot']['workers'][0]['has_error'])

    def test_success_reports_only_after_outer_rollback_and_uses_verified_tls(self):
        conn = Connection()
        output, error, _, driver = self.run_with_sdk(conn)
        self.assertIsNone(error)
        self.assertEqual(json.loads(output)['result'], 'OPERATIONS_PERMISSIONS_OK')
        self.assertFalse(conn.committed)
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertNotIn('FAKE_TOKEN_NEVER_PRINT', output)
        self.assertNotIn('DO_NOT_LOG_RAW', output)

    def test_failed_positive_query_rolls_back_and_never_prints_success(self):
        conn = Connection()
        conn.fail_aggregate = True
        output, error, _, _ = self.run_with_sdk(conn)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(output, '')
        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(conn.committed)

    def test_wrong_client_never_requests_token_or_opens_database(self):
        output, error, identity, driver = self.run_with_sdk(Connection(), wrong_client=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(output, '')
        identity.WorkloadIdentityCredential.assert_not_called()
        driver.connect.assert_not_called()
