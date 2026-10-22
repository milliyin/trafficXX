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
spec = importlib.util.spec_from_file_location('publisher_permissions', ROOT / 'scripts/check-publisher-permissions.py')
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
        self.row = dict(id=123, publish_attempts=2, publish_failures=1,
                        message_id='stable-message', payload={'event_id': 'demo-seated'},
                        last_error=None, marked=False, delayed=False, attempted=False)
        self.beat = None
        self.dirty = self.committed = False
        self.rollbacks = 0
        self.fixture = True
        self.zero_update = False
        self.fail_heartbeat = False
        self.allow_forbidden = False
        self.error_state = '42501'

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
        self.beat = None
        self.row.update(publish_attempts=2, publish_failures=1, marked=False,
                        last_error=None, delayed=False, attempted=False)

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql in module.DENIED.values():
            if self.allow_forbidden:
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
        if sql.startswith('SELECT * FROM public.outbox'):
            return Cursor(self.row.copy() if self.fixture else None)
        if sql.startswith('UPDATE public.outbox'):
            self.dirty = True
            if self.zero_update:
                return Cursor(rowcount=0)
            if 'SET publish_attempts' in sql:
                self.row['publish_attempts'] += 1
                self.row['attempted'] = True
            elif 'SET publish_failures' in sql:
                self.row['publish_failures'] += 1
                self.row.update(last_error='BROKER_SEND_FAILED', delayed=True)
            else:
                self.row.update(marked=True, last_error=None)
            return Cursor()
        if sql.startswith('SELECT publish_attempts') or sql.startswith('SELECT published_at'):
            return Cursor(self.row.copy())
        if sql.startswith('INSERT INTO public.worker_heartbeats'):
            if self.fail_heartbeat:
                raise SqlError()
            self.dirty = True
            self.beat = dict(name=params[0], last_error=params[1])
            return Cursor()
        if sql.startswith('SELECT name, last_error FROM public.worker_heartbeats'):
            return Cursor(self.beat.copy())
        raise AssertionError(sql)


class PublisherPermissions(unittest.TestCase):
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
        claims = base64.urlsafe_b64encode(json.dumps({'sub': 'system:serviceaccount:trafficxx:publisher'}).encode()).decode().rstrip('=')
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

    def test_publication_retry_upsert_and_twenty_three_denials(self):
        conn = Connection()
        result = module.verify_permissions(conn)
        self.assertEqual(len(result['allowed']), 9)
        self.assertEqual(len(result['denied']), 23)
        self.assertEqual(result['broker_messages_sent'], 0)
        self.assertFalse(result['diagnostic_rows_committed'])
        self.assertEqual(conn.row['publish_attempts'], 3)
        self.assertTrue(conn.row['marked'])
        self.assertEqual(conn.beat['last_error'], 'PROBE_ERROR')
        inserts = [s for s in conn.statements if s.startswith('INSERT INTO public.worker_heartbeats')
                   and s not in module.DENIED.values()]
        self.assertEqual(len(inserts), 2)
        self.assertTrue(all('ON CONFLICT (name) DO UPDATE' in s for s in inserts))

    def test_every_unexpected_denial_success_is_rolled_back(self):
        for name, sql in module.DENIED.items():
            with self.subTest(name=name):
                conn = Connection()
                conn.allow_forbidden = True
                with self.assertRaises(ValueError):
                    module.expect_denied(conn, name, sql)
                self.assertEqual(conn.statements[-2:], ['ROLLBACK TO SAVEPOINT permission_probe',
                                                       'RELEASE SAVEPOINT permission_probe'])

    def test_wrong_sqlstate_is_not_a_permission_denial(self):
        conn = Connection()
        conn.error_state = '23503'
        with self.assertRaises(SqlError):
            module.expect_denied(conn, 'outbox_insert', module.DENIED['outbox_insert'])

    def test_no_pending_fixture_does_not_invent_messages(self):
        conn = Connection()
        conn.fixture = False
        with self.assertRaisesRegex(ValueError, 'No pending due outbox'):
            module.positive_checks(conn)
        self.assertFalse(conn.dirty)

    def test_zero_attempt_update_fails_before_heartbeat(self):
        conn = Connection()
        conn.zero_update = True
        with self.assertRaises(ValueError):
            module.positive_checks(conn)
        self.assertIsNone(conn.beat)

    def test_sdk_success_restores_existing_outbox_and_removes_heartbeat(self):
        conn = Connection()
        output, error, _, driver = self.run_with_sdk(conn)
        self.assertIsNone(error)
        self.assertEqual(json.loads(output)['result'], 'PUBLISHER_PERMISSIONS_OK')
        self.assertEqual(conn.row['publish_attempts'], 2)
        self.assertFalse(conn.row['marked'])
        self.assertIsNone(conn.beat)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertNotIn('FAKE_TOKEN_NEVER_PRINT', output)

    def test_failed_heartbeat_rolls_back_staged_publication_without_success(self):
        conn = Connection()
        conn.fail_heartbeat = True
        output, error, _, _ = self.run_with_sdk(conn)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(output, '')
        self.assertEqual(module.CURRENT_CHECK, 'allowed.heartbeat_insert')
        self.assertEqual(conn.row['publish_attempts'], 2)
        self.assertFalse(conn.row['marked'])
        self.assertFalse(conn.committed)
        self.assertEqual(conn.rollbacks, 1)

    def test_wrong_client_never_opens_sql_or_requests_token(self):
        output, error, identity, driver = self.run_with_sdk(Connection(), wrong_client=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(output, '')
        identity.WorkloadIdentityCredential.assert_not_called()
        driver.connect.assert_not_called()

