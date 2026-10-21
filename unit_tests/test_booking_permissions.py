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
spec = importlib.util.spec_from_file_location('booking_permissions', ROOT / 'scripts/check-booking-permissions.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SqlError(Exception):
    sqlstate = '42501'


class Cursor:
    def __init__(self, row=None, rowcount=1):
        self.row, self.rowcount = row, rowcount

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self):
        self.statements = []
        self.reservation, self.assignment, self.outbox = None, None, []
        self.dirty, self.committed, self.rollbacks = False, False, 0
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
            return Cursor({'inventory_type': 'GENERAL'})
        if sql.startswith('UPDATE public.events'):
            self.dirty = True
            return Cursor()
        if sql.startswith('SELECT unit_number'):
            return Cursor({'unit_number': 1} if self.free else None)
        if 'INSERT INTO public.reservations' in sql:
            self.dirty = True
            self.reservation = dict(id=params[0], status='HELD', quantity=1, request=json.loads(params[4]))
            return Cursor(self.reservation.copy())
        if sql.startswith('SELECT * FROM public.reservations'):
            return Cursor(self.reservation.copy())
        if sql.startswith('UPDATE public.ticket_units SET reservation_id = %s'):
            self.assignment = params[0]
            return Cursor(rowcount=0 if self.zero_claim else 1)
        if sql.startswith('INSERT INTO public.outbox'):
            if self.fail_outbox:
                raise SqlError()
            self.outbox.append(params)
            return Cursor()
        if sql.startswith('UPDATE public.reservations'):
            self.reservation['status'] = 'CANCELLED'
            return Cursor(self.reservation.copy())
        if sql.startswith('UPDATE public.ticket_units SET reservation_id = NULL'):
            self.assignment = None
            return Cursor()
        raise AssertionError(sql)


class BookingPermissions(unittest.TestCase):
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
        claims = base64.urlsafe_b64encode(json.dumps({'sub': 'system:serviceaccount:trafficxx:booking'}).encode()).decode().rstrip('=')
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

    def test_all_required_sql_operations_and_twelve_denials(self):
        connection = Connection()
        result = module.verify_permissions(connection)
        self.assertEqual(len(result['allowed']), 11)
        self.assertEqual(len(result['denied']), 12)
        self.assertFalse(result['diagnostic_rows_committed'])
        self.assertEqual(len(connection.outbox), 2)
        self.assertIsNone(connection.assignment)
        self.assertTrue(all('RETURNING' not in sql for sql in connection.statements
                            if sql.startswith('INSERT INTO public.outbox')))

    def test_each_unexpected_allowed_operation_is_rolled_back_then_rejected(self):
        for name, sql in module.DENIED.items():
            with self.subTest(name=name):
                connection = Connection()
                connection.allow_forbidden = True
                with self.assertRaises(ValueError):
                    module.expect_denied(connection, name, sql)
                self.assertEqual(connection.statements[-2:], ['ROLLBACK TO SAVEPOINT permission_probe',
                                                             'RELEASE SAVEPOINT permission_probe'])

    def test_other_error_codes_are_not_classified_as_permission_denials(self):
        for state in ('25006', '42601', '42P01', None):
            with self.subTest(state=state):
                connection = Connection()
                connection.error_state = state
                with self.assertRaises(SqlError):
                    module.expect_denied(connection, 'create_table', module.DENIED['create_table'])

    def test_no_free_inventory_stops_without_inserting_reservations(self):
        connection = Connection()
        connection.free = False
        with self.assertRaisesRegex(ValueError, 'No free demo unit'):
            module.positive_checks(connection)
        self.assertIsNone(connection.reservation)

    def test_failed_ticket_claim_stops_before_outbox(self):
        connection = Connection()
        connection.zero_claim = True
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            module.positive_checks(connection)
        self.assertEqual(connection.outbox, [])

    def test_outbox_error_keeps_the_correct_safe_check_name(self):
        connection = Connection()
        connection.fail_outbox = True
        with self.assertRaises(SqlError):
            module.positive_checks(connection)
        self.assertEqual(module.CURRENT_CHECK, 'allowed.outbox_held')

    def test_successful_sdk_probe_rolls_back_all_rows_before_success_output(self):
        connection = Connection()
        output, error, identity, psycopg = self.run_with_sdk(connection)
        self.assertIsNone(error)
        self.assertEqual(json.loads(output)['result'], 'BOOKING_PERMISSIONS_OK')
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.committed)
        self.assertIsNone(connection.reservation)
        self.assertEqual(connection.outbox, [])
        self.assertNotIn('FAKE_TOKEN_NEVER_PRINT', output)
        self.assertEqual(psycopg.connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertEqual(psycopg.connect.call_args.kwargs['user'], module.ROLE)
        identity.WorkloadIdentityCredential.assert_called_once()

    def test_outbox_failure_rolls_back_partial_diagnostic_rows_and_prints_no_success(self):
        connection = Connection()
        connection.fail_outbox = True
        output, error, _, _ = self.run_with_sdk(connection)
        self.assertIsInstance(error, SqlError)
        self.assertEqual(output, '')
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(connection.committed)
        self.assertIsNone(connection.assignment)

    def test_wrong_client_never_requests_a_token_or_opens_sql(self):
        output, error, identity, psycopg = self.run_with_sdk(Connection(), wrong_client=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(output, '')
        identity.WorkloadIdentityCredential.assert_not_called()
        psycopg.connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
