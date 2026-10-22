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
spec = importlib.util.spec_from_file_location('expiry_permissions', ROOT / 'scripts/check-expiry-permissions.py')
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
        self.reservation, self.assignment, self.outbox = dict(id='00000000-0000-0000-0000-000000000123', status='EXPIRED'), None, []
        self.dirty, self.committed, self.rollbacks = False, False, 0
        self.fixture = True
        self.bound = False
        self.zero_expire = False
        self.free = True
        self.zero_claim = False
        self.fail_outbox = False
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
        self.reservation, self.assignment, self.outbox = None, None, []
        self.dirty = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql in module.DENIED.values():
            if self.allow_forbidden:
                return Cursor()
            error = SqlError()
            error.sqlstate = self.error_state
            raise error
        if sql.startswith(('SAVEPOINT', 'ROLLBACK TO', 'RELEASE', 'SET TRANSACTION', 'SELECT pg_advisory')):
            return Cursor()
        if 'FROM pg_stat_ssl' in sql:
            return Cursor(dict(database='trafficxx', identity=module.ROLE, ssl=True, version='TLSv1.3',
                               rolsuper=False, rolcreatedb=False, rolcreaterole=False, rolreplication=False,
                               rolbypassrls=False, admin=False))
        if sql.startswith('SELECT * FROM public.events'):
            return Cursor({'inventory_type': 'SEATED'})
        if sql.startswith('UPDATE public.events'):
            self.dirty = True
            return Cursor()
        if sql.startswith('SELECT * FROM public.reservations'):
            return Cursor(self.reservation.copy() if self.fixture else None)
        if sql.startswith('SELECT reservation_id FROM public.ticket_units'):
            return Cursor({'reservation_id': self.reservation['id']} if self.bound else None)
        if sql.startswith('SELECT DISTINCT event_id'):
            return Cursor([{'event_id': 'demo-seated'}])
        if sql.startswith('UPDATE public.reservations'):
            self.dirty = True
            self.reservation['status'] = 'HELD' if "SET status = 'HELD'" in sql else 'EXPIRED'
            if self.zero_expire and self.reservation['status'] == 'EXPIRED':
                return Cursor(None)
            return Cursor(self.reservation.copy())
        if sql.startswith('UPDATE public.ticket_units'):
            return Cursor(rowcount=0)
        if sql.startswith('INSERT INTO public.outbox'):
            if self.fail_outbox:
                raise SqlError()
            self.outbox.append(params)
            return Cursor()
        raise AssertionError(sql)




class ExpiryPermissions(unittest.TestCase):
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
        claims = base64.urlsafe_b64encode(json.dumps({'sub': 'system:serviceaccount:trafficxx:expiry'}).encode()).decode().rstrip('=')
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

    def test_required_queries_and_fourteen_denials(self):
        conn = Connection()
        result = module.verify_permissions(conn)
        self.assertEqual(len(result['allowed']), 9)
        self.assertEqual(len(result['denied']), 14)
        self.assertEqual(conn.reservation['status'], 'EXPIRED')
        self.assertEqual(len(conn.outbox), 1)
        self.assertTrue(conn.outbox[0][1].startswith('__permission_probe__'))

    def test_every_unexpected_success_is_rolled_back_and_rejected(self):
        for name, sql in module.DENIED.items():
            with self.subTest(name=name):
                conn = Connection()
                conn.allow_forbidden = True
                with self.assertRaises(ValueError):
                    module.expect_denied(conn, name, sql)
                self.assertEqual(conn.statements[-2:], ['ROLLBACK TO SAVEPOINT permission_probe',
                                                        'RELEASE SAVEPOINT permission_probe'])

    def test_other_sqlstates_are_not_permission_proof(self):
        conn = Connection()
        conn.error_state = '23505'
        with self.assertRaises(SqlError):
            module.expect_denied(conn, 'outbox_read', module.DENIED['outbox_read'])

    def test_missing_or_bound_fixture_stops_before_status_staging(self):
        for mode in ('missing', 'bound'):
            with self.subTest(mode=mode):
                conn = Connection()
                conn.fixture = mode != 'missing'
                conn.bound = mode == 'bound'
                with self.assertRaises(ValueError):
                    module.positive_checks(conn)
                self.assertFalse(any('UPDATE public.reservations' in sql for sql in conn.statements))

    def test_failed_expiry_update_stops_before_outbox(self):
        conn = Connection()
        conn.zero_expire = True
        with self.assertRaises(ValueError):
            module.positive_checks(conn)
        self.assertEqual(conn.outbox, [])

    def test_sdk_success_rolls_back_before_printing(self):
        conn = Connection()
        output, error, _, driver = self.run_with_sdk(conn)
        self.assertIsNone(error)
        self.assertEqual(json.loads(output)['result'], 'EXPIRY_PERMISSIONS_OK')
        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(conn.committed)
        self.assertEqual(conn.outbox, [])
        self.assertEqual(driver.connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertNotIn('FAKE_TOKEN_NEVER_PRINT', output)

    def test_outbox_failure_rolls_back_before_any_success(self):
        conn = Connection()
        conn.fail_outbox = True
        output, error, _, _ = self.run_with_sdk(conn)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(module.CURRENT_CHECK, 'allowed.outbox_insert')
        self.assertEqual(output, '')
        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(conn.committed)

    def test_wrong_identity_never_requests_token_or_sql(self):
        output, error, identity, driver = self.run_with_sdk(Connection(), wrong_client=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(output, '')
        identity.WorkloadIdentityCredential.assert_not_called()
        driver.connect.assert_not_called()

