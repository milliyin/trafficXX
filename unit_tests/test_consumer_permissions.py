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
spec = importlib.util.spec_from_file_location('consumer_permissions', ROOT / 'scripts/check-consumer-permissions.py')
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
        self.inbox = {}
        self.projection = None
        self.beat = None
        self.dirty = self.committed = False
        self.rollbacks = 0
        self.fixture = True
        self.allow_forbidden = False
        self.error_state = '42501'
        self.fail_failure_insert = False
        self.bad_payload = self.bad_ordering = False
        self.dead_letters = set()

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
        self.inbox.clear()
        self.projection = self.beat = None
        self.dead_letters.clear()

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
        if sql.startswith('INSERT INTO public.consumer_inbox'):
            self.dirty = True
            if params[0] in self.inbox:
                return Cursor(None, 0)
            self.inbox[params[0]] = json.loads(params[1])
            return Cursor({'message_id': params[0]})
        if sql.startswith('SELECT envelope'):
            return Cursor({'envelope': {} if self.bad_payload else self.inbox[params[0]]})
        if sql.startswith('SELECT last_outbox_id'):
            return Cursor(self.projection.copy() if self.projection else None)
        if sql.startswith('INSERT INTO public.reservation_updates'):
            if not self.fixture:
                error = SqlError()
                error.sqlstate = '23503'
                raise error
            if self.projection and self.projection['last_outbox_id'] >= params[3] and not self.bad_ordering:
                return Cursor(rowcount=0)
            self.dirty = True
            self.projection = dict(zip(('reservation_id', 'event_id', 'status', 'last_outbox_id'), params))
            return Cursor()
        if sql.startswith('SELECT reservation_id'):
            return Cursor(self.projection.copy())
        if sql.startswith('INSERT INTO public.worker_heartbeats'):
            self.dirty = True
            self.beat = dict(name=params[0], last_error=params[1])
            return Cursor()
        if sql.startswith('SELECT name, last_error'):
            return Cursor(self.beat.copy())
        if sql.startswith('INSERT INTO public.worker_failures'):
            if self.fail_failure_insert:
                raise SqlError()
            self.dirty = True
            return Cursor()
        if sql.startswith('INSERT INTO public.observed_dead_letters'):
            key = params[:2]
            if key in self.dead_letters:
                return Cursor(rowcount=0)
            self.dead_letters.add(key)
            self.dirty = True
            return Cursor()
        raise AssertionError(sql)


class ConsumerPermissions(unittest.TestCase):
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
        claims = base64.urlsafe_b64encode(json.dumps({'sub': 'system:serviceaccount:trafficxx:consumer'}).encode()).decode().rstrip('=')
        path = MagicMock()
        path.read_text.return_value = 'header.' + claims + '.signature'
        path.is_file.return_value = True
        env = dict(AZURE_CLIENT_ID='wrong' if wrong_client else module.CLIENT,
                   AZURE_TENANT_ID=module.TENANT, AZURE_FEDERATED_TOKEN_FILE='/fake/projection',
                   PROBE_RESERVATION_ID='2a6f5566-9870-45aa-9927-a85fd8e5f3a9')
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

    def test_inbox_projection_ordering_and_all_denials(self):
        conn = Connection()
        with patch.dict(os.environ, PROBE_RESERVATION_ID='2a6f5566-9870-45aa-9927-a85fd8e5f3a9'):
            result = module.verify_permissions(conn)
        self.assertEqual(len(result['allowed']), 14)
        self.assertEqual(len(result['denied']), 30)
        self.assertEqual(result['broker_messages_received'], 0)
        self.assertEqual(len(conn.inbox), 1)
        self.assertEqual(conn.projection['status'], 'EXPIRED')
        self.assertEqual(conn.projection['last_outbox_id'], 2)
        self.assertEqual(len(conn.dead_letters), 1)
        self.assertEqual(conn.beat['last_error'], 'PROBE_ERROR')

    def test_every_unexpected_allowed_operation_is_rolled_back_to_savepoint(self):
        for name, sql in module.DENIED.items():
            with self.subTest(name=name):
                conn = Connection()
                conn.allow_forbidden = True
                with self.assertRaises(ValueError):
                    module.expect_denied(conn, name, sql)
                self.assertEqual(conn.statements[-2:], ['ROLLBACK TO SAVEPOINT permission_probe',
                                                       'RELEASE SAVEPOINT permission_probe'])

    def test_non_permission_sqlstate_is_not_counted_as_denial(self):
        conn = Connection()
        conn.error_state = '23503'
        with self.assertRaises(SqlError):
            module.expect_denied(conn, 'reservation_read', module.DENIED['reservation_read'])

    def test_missing_fk_fixture_rolls_back_inbox_and_never_prints_success(self):
        conn = Connection()
        conn.fixture = False
        output, error, _, _ = self.run_with_sdk(conn)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(error.sqlstate, '23503')
        self.assertEqual(output, '')
        self.assertFalse(conn.inbox)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.rollbacks, 1)

    def test_success_rolls_back_all_rows_before_reporting(self):
        conn = Connection()
        output, error, _, driver = self.run_with_sdk(conn)
        self.assertIsNone(error)
        self.assertEqual(json.loads(output)['result'], 'CONSUMER_PERMISSIONS_OK')
        self.assertFalse(conn.inbox)
        self.assertIsNone(conn.projection)
        self.assertIsNone(conn.beat)
        self.assertFalse(conn.dead_letters)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertNotIn('FAKE_TOKEN_NEVER_PRINT', output)

    def test_failure_insert_error_rolls_back_partial_success(self):
        conn = Connection()
        conn.fail_failure_insert = True
        output, error, _, _ = self.run_with_sdk(conn)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(output, '')
        self.assertEqual(module.CURRENT_CHECK, 'allowed.failure_insert')
        self.assertFalse(conn.inbox)
        self.assertIsNone(conn.projection)
        self.assertEqual(conn.rollbacks, 1)

    def test_bad_payload_or_ordering_fails_without_success(self):
        for flag in ('bad_payload', 'bad_ordering'):
            with self.subTest(flag=flag):
                conn = Connection()
                setattr(conn, flag, True)
                output, error, _, _ = self.run_with_sdk(conn)
                self.assertIsInstance(error, ValueError)
                self.assertEqual(output, '')
                self.assertFalse(conn.inbox)
                self.assertFalse(conn.committed)

    def test_wrong_client_does_not_request_token_or_open_sql(self):
        output, error, identity, driver = self.run_with_sdk(Connection(), wrong_client=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(output, '')
        identity.WorkloadIdentityCredential.assert_not_called()
        driver.connect.assert_not_called()
