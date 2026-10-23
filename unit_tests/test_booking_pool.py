"""Connection-provider and transaction-boundary decisions without a database."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class BookingPoolTransactions(unittest.TestCase):
    def setUp(self):
        api, json_types, db = [ModuleType(name) for name in
                               ('fastapi', 'psycopg.types.json', 'trafficxx.db')]
        api.HTTPException = HttpError
        json_types.Jsonb = lambda value: value
        self.direct = db.connect = MagicMock()
        spec = importlib.util.spec_from_file_location(
            'isolated_booking_service', Path(__file__).resolve().parents[1] / 'trafficxx/service.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'fastapi': api, 'psycopg.types.json': json_types, 'trafficxx.db': db}):
            spec.loader.exec_module(self.module)
        self.conn = MagicMock()
        self.events = []
        self.factory = MagicMock(side_effect=self.lease)
        self.direct.side_effect = self.lease
        self.row = {'id': 'reservation', 'event_id': 'general', 'status': 'HELD',
                    'quantity': 1, 'request': {'event_id': 'general', 'quantity': 1},
                    'created_at': 'created', 'expires_at': 'expiry'}
        self.body = SimpleNamespace(event_id='general', quantity=1, seats=None,
                                    model_dump=lambda **kwargs: self.row['request'])

    @contextmanager
    def lease(self):
        self.events.append('borrow')
        try:
            yield self.conn
        except BaseException:
            self.events.append('rollback')
            raise
        else:
            self.events.append('commit')
        finally:
            self.events.append('return')

    def reservation_sql(self, query, parameters=None):
        cursor = MagicMock()
        if query.startswith('SELECT * FROM reservations'):
            cursor.fetchone.return_value = dict(self.row)
        elif query.startswith('SELECT * FROM events'):
            cursor.fetchone.return_value = {'id': 'general', 'inventory_type': 'GENERAL'}
        elif query.startswith('UPDATE reservations SET status'):
            cursor.fetchall.return_value = []
        return cursor

    def test_lookup_uses_injected_transaction_and_no_direct_login(self):
        self.conn.execute.side_effect = self.reservation_sql
        result = self.module.reservation('alice', 'reservation', connection_factory=self.factory)
        self.assertEqual(result['status'], 'HELD')
        self.assertEqual(self.events, ['borrow', 'commit', 'return'])
        self.direct.assert_not_called()
        self.factory.assert_called_once_with()

    def test_missing_customer_reservation_rolls_back_then_returns_connection(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with self.assertRaises(HttpError) as error:
            self.module.reservation('alice', 'missing', connection_factory=self.factory)
        self.assertEqual(error.exception.detail, {'code': 'RESERVATION_NOT_FOUND'})
        self.assertEqual(self.events, ['borrow', 'rollback', 'return'])
        self.direct.assert_not_called()

    def test_expired_action_conflict_is_raised_after_commit_and_return(self):
        self.row['status'] = 'EXPIRED'
        self.conn.execute.side_effect = self.reservation_sql
        with self.assertRaises(HttpError) as error:
            self.module.reservation('alice', 'reservation', 'CONFIRMED', connection_factory=self.factory)
        self.assertEqual(error.exception.detail, {'code': 'RESERVATION_EXPIRED'})
        self.assertEqual(self.events, ['borrow', 'commit', 'return'])

    def test_default_service_and_sweep_keep_direct_connection_path(self):
        self.conn.execute.side_effect = self.reservation_sql
        self.module.reservation('alice', 'reservation')
        self.direct.assert_called_once_with()
        self.direct.reset_mock()
        self.conn.execute.side_effect = None
        self.conn.execute.return_value.fetchall.return_value = []
        self.assertEqual(self.module.sweep(), 0)
        self.direct.assert_called_once_with()

    def test_outbox_exception_reaches_same_transaction_without_replay(self):
        def execute(query, parameters=None):
            cursor = self.reservation_sql(query, parameters)
            if query.startswith('SELECT * FROM reservations WHERE customer_id'):
                cursor.fetchone.return_value = None
            elif query.startswith('SELECT unit_number'):
                cursor.fetchall.return_value = [{'unit_number': 1}]
            elif query.startswith('INSERT INTO reservations'):
                cursor.fetchone.return_value = self.row
            elif query.startswith('INSERT INTO outbox'):
                raise RuntimeError('Injected failure')
            return cursor

        self.conn.execute.side_effect = execute
        with self.assertRaises(RuntimeError):
            self.module.reserve('alice', 'key', self.body, connection_factory=self.factory)
        self.assertEqual(self.events, ['borrow', 'rollback', 'return'])
        self.factory.assert_called_once_with()
        self.direct.assert_not_called()

    def test_changed_idempotent_request_rolls_back_without_second_borrow(self):
        previous = dict(self.row, request={'event_id': 'general', 'quantity': 2})
        self.conn.execute.return_value.fetchone.return_value = previous
        with self.assertRaises(HttpError) as error:
            self.module.reserve('alice', 'key', self.body, connection_factory=self.factory)
        self.assertEqual(error.exception.detail, {'code': 'IDEMPOTENCY_KEY_REUSED'})
        self.assertEqual(self.events, ['borrow', 'rollback', 'return'])
        self.factory.assert_called_once_with()
