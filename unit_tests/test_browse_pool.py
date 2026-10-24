"""Offline pool wiring, backpressure and lifecycle tests; no live database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class SqlError(Exception):
    pass


class PoolTimeout(Exception):
    pass


class TooManyRequests(Exception):
    pass


class PoolClosed(Exception):
    pass


class BrowsePool(unittest.TestCase):
    def setUp(self):
        api, driver, pools, db = [ModuleType(name) for name in
                                  ('fastapi', 'psycopg', 'psycopg_pool', 'trafficxx.db')]
        api.HTTPException, driver.OperationalError = HttpError, SqlError
        pools.PoolTimeout, pools.TooManyRequests, pools.PoolClosed = (
            PoolTimeout, TooManyRequests, PoolClosed)
        self.factory = pools.ConnectionPool = MagicMock()
        self.pool = self.factory.return_value
        self.parameters = db.connection_parameters = MagicMock(return_value={'password': 'TOKEN'})
        spec = importlib.util.spec_from_file_location(
            'isolated_database_pool', Path(__file__).resolve().parents[1] / 'trafficxx/database_pool.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'fastapi': api, 'psycopg': driver,
                                       'psycopg_pool': pools, 'trafficxx.db': db}):
            spec.loader.exec_module(self.module)
        self.database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX')

    def test_pool_is_bounded_and_does_not_fetch_startup_token(self):
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4'}):
            self.database.open()
        self.parameters.assert_not_called()
        options = self.factory.call_args.kwargs
        self.assertEqual((options['min_size'], options['max_size'], options['max_waiting']), (1, 4, 8))
        self.assertEqual((options['timeout'], options['max_lifetime']), (3, 1800))
        self.assertIs(options['check'], self.factory.check_connection)
        self.pool.open.assert_called_once_with(wait=False)
        self.pool.wait.assert_not_called()
        self.database.close()
        self.database.close()
        self.pool.close.assert_called_once()

    def test_warm_floor_waits_for_initial_connections_and_keeps_existing_ceiling(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4', 'BROWSE_DB_POOL_MIN': '4'}):
            database.open()
        options = self.factory.call_args.kwargs
        self.assertEqual((options['min_size'], options['max_size'], options['max_waiting']), (4, 4, 8))
        self.pool.wait.assert_called_once_with(timeout=30)
        self.assertIs(database.pool, self.pool)

    def test_reviewed_eight_slot_policy_is_ready_before_serving(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN',
                                            'BROWSE_DB_POOL_MAX_WAITING')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '8', 'BROWSE_DB_POOL_MIN': '8',
                                    'BROWSE_DB_POOL_MAX_WAITING': '16'}):
            database.open()
        options = self.factory.call_args.kwargs
        self.assertEqual((options['min_size'], options['max_size'], options['max_waiting']), (8, 8, 16))
        self.assertEqual(options['timeout'], 3)
        self.pool.wait.assert_called_once_with(timeout=30)

    def test_warm_startup_timeout_closes_pool_without_admitting_requests(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN')
        self.pool.wait.side_effect = PoolTimeout('PRIVATE')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4', 'BROWSE_DB_POOL_MIN': '4'}), \
                self.assertRaises(PoolTimeout):
            database.open()
        self.pool.close.assert_called_once()
        self.assertIsNone(database.pool)

    def test_invalid_minimum_fails_before_connecting_and_booking_ignores_browse_floor(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN')
        for minimum in ('0', '5', 'invalid', '-1'):
            with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4', 'BROWSE_DB_POOL_MIN': minimum}), \
                    self.assertRaises(ValueError):
                database.open()
        self.factory.assert_not_called()
        booking = self.module.DatabasePool('booking', 'BOOKING_DB_POOL_MAX')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MIN': '4', 'BOOKING_DB_POOL_MAX': '4'}):
            booking.open()
        self.assertEqual(self.factory.call_args.kwargs['min_size'], 1)
        self.pool.wait.assert_not_called()

    def test_reviewed_waiting_limit_is_bounded_and_independent_from_booking(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN',
                                            'BROWSE_DB_POOL_MAX_WAITING')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4', 'BROWSE_DB_POOL_MIN': '4',
                                    'BROWSE_DB_POOL_MAX_WAITING': '16'}):
            database.open()
            self.assertEqual(self.factory.call_args.kwargs['max_waiting'], 16)
            self.assertEqual(self.factory.call_args.kwargs['max_size'], 4)
            self.pool.get_stats.return_value = {}
            self.assertEqual(database.statistics()['statistics']['pool_waiting_limit'], 16)
            booking = self.module.DatabasePool('booking', 'BOOKING_DB_POOL_MAX')
            booking.open()
            self.assertEqual(self.factory.call_args.kwargs['max_waiting'], 8)

    def test_unlimited_or_unreviewed_queue_sizes_fail_before_connecting(self):
        database = self.module.DatabasePool('browse', 'BROWSE_DB_POOL_MAX', waiting_variable='BROWSE_DB_POOL_MAX_WAITING')
        for value in ('0', '-1', '9', '17', 'unlimited'):
            with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX_WAITING': value}), self.assertRaises(ValueError):
                database.open()
        self.factory.assert_not_called()

    def test_new_connection_parameters_are_resolved_each_time(self):
        self.parameters.side_effect = [{'password': 'FIRST'}, {'password': 'NEW'}]
        self.assertEqual(self.module.pool_parameters(), {'password': 'FIRST'})
        self.assertEqual(self.module.pool_parameters(), {'password': 'NEW'})

    def test_booking_capacity_is_independent_from_browse(self):
        booking = self.module.DatabasePool('booking', 'BOOKING_DB_POOL_MAX')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '8', 'BOOKING_DB_POOL_MAX': '2'}):
            booking.open()
            self.assertEqual(self.factory.call_args.kwargs['max_size'], 2)
            self.assertEqual(self.factory.call_args.kwargs['name'], 'booking')
            self.database.open()
            self.assertEqual(self.factory.call_args.kwargs['max_size'], 8)
            self.assertEqual(self.factory.call_args.kwargs['name'], 'browse')

    def test_url_is_passed_once_and_cloud_conninfo_is_empty(self):
        self.parameters.return_value = {'conninfo': 'postgresql://local/test', 'connect_timeout': 5}
        with patch.dict(os.environ, {'DATABASE_AUTH_MODE': 'url', 'DATABASE_URL': 'postgresql://local/test'}):
            self.assertEqual(self.module.pool_conninfo(), 'postgresql://local/test')
            self.assertEqual(self.module.pool_parameters(), {'connect_timeout': 5})
        with patch.dict(os.environ, {'DATABASE_AUTH_MODE': 'workload_identity'}):
            self.assertEqual(self.module.pool_conninfo(), '')

    def test_invalid_size_fails_before_pool_creation(self):
        for value in ('0', '9', 'abc', '-1'):
            with self.subTest(value=value), patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': value}):
                with self.assertRaises(ValueError):
                    self.database.open()
        self.factory.assert_not_called()

    def test_capacity_errors_are_sanitized_503(self):
        self.database.pool = self.pool
        for error in (PoolTimeout, TooManyRequests, PoolClosed):
            with self.subTest(error=error):
                self.pool.connection.side_effect = error('PRIVATE RAW ERROR')
                with self.assertRaises(HttpError) as caught:
                    with self.database.connection():
                        self.fail('must not yield')
                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.detail, {'code': 'DATABASE_BUSY'})

    def test_rejection_types_are_separate_numeric_and_leave_public_response_unchanged(self):
        self.database.pool = self.pool
        self.pool.get_stats.return_value = {}
        for error, key in ((TooManyRequests, 'rejections_queue_full'),
                           (PoolTimeout, 'rejections_timeout'), (PoolClosed, 'rejections_closed')):
            self.pool.connection.side_effect = error('PRIVATE')
            with self.assertRaises(HttpError) as caught:
                with self.database.connection():
                    self.fail('must not yield')
            self.assertEqual(caught.exception.detail, {'code': 'DATABASE_BUSY'})
            self.assertEqual(self.database.statistics()['statistics'][key], 1)
        values = self.database.statistics()['statistics']
        self.assertEqual([values[key] for key in ('rejections_queue_full', 'rejections_timeout', 'rejections_closed')], [1, 1, 1])
        self.assertNotIn('PRIVATE', str(values))
        self.pool.pop_stats.assert_not_called()

    def test_concurrent_rejections_do_not_lose_increments(self):
        self.database.pool = self.pool
        self.pool.get_stats.return_value = {}
        self.pool.connection.side_effect = TooManyRequests('PRIVATE')
        def borrow(index):
            try:
                with self.database.connection():
                    self.fail('must not yield')
            except HttpError as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=8) as executor:
            codes = list(executor.map(borrow, range(100)))
        self.assertEqual(codes, [503] * 100)
        values = self.database.statistics()['statistics']
        self.assertEqual(values['rejections_queue_full'], 100)
        self.assertEqual(values['rejections_timeout'], 0)
        self.assertEqual(values['rejections_closed'], 0)

    def test_request_failure_reaches_pool_context_for_rollback(self):
        self.database.pool = self.pool
        seen = []

        @contextmanager
        def lease():
            try:
                yield 'connection'
            except ValueError:
                seen.append('rollback')
                raise
            finally:
                seen.append('return')

        self.pool.connection.side_effect = lease
        with self.assertRaises(ValueError):
            with self.database.connection() as conn:
                self.assertEqual(conn, 'connection')
                raise ValueError('request failed')
        self.assertEqual(seen, ['rollback', 'return'])

    def test_unstarted_pool_does_not_fall_back_to_direct_login(self):
        with self.assertRaises(SqlError):
            with self.database.connection():
                self.fail('must not yield')
        self.parameters.assert_not_called()

    def test_partial_startup_closes_pool(self):
        self.pool.open.side_effect = RuntimeError('startup failed')
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4'}), self.assertRaises(RuntimeError):
            self.database.open()
        self.pool.close.assert_called_once()
        self.assertIsNone(self.database.pool)

    def test_statistics_are_allowlisted_and_do_not_reset_or_borrow(self):
        self.database.pool = self.pool
        self.pool.get_stats.return_value = {'pool_size': 2, 'requests_num': 40,
                                           'password': 'MUST_NOT_EXPOSE', 'conninfo': 'PRIVATE'}
        row = self.database.statistics()
        self.assertEqual(len(row['instance_id']), 32)
        self.assertEqual(row['statistics']['pool_size'], 2)
        self.assertEqual(row['statistics']['connections_num'], 0)
        self.assertNotIn('MUST_NOT_EXPOSE', str(row))
        self.assertNotIn('conninfo', row['statistics'])
        self.pool.pop_stats.assert_not_called()
        self.pool.connection.assert_not_called()

    def test_statistics_before_startup_return_sanitized_503(self):
        with self.assertRaises(HttpError) as caught:
            self.database.statistics()
        self.assertEqual(caught.exception.status_code, 503)
