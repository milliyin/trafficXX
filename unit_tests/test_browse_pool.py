"""Offline pool wiring, backpressure and lifecycle tests; no live database."""
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
            'isolated_browse_pool', Path(__file__).resolve().parents[1] / 'trafficxx/browse_db.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'fastapi': api, 'psycopg': driver,
                                       'psycopg_pool': pools, 'trafficxx.db': db}):
            spec.loader.exec_module(self.module)
        self.database = self.module.BrowseDatabase()

    def test_pool_is_bounded_and_does_not_fetch_startup_token(self):
        with patch.dict(os.environ, {'BROWSE_DB_POOL_MAX': '4'}):
            self.database.open()
        self.parameters.assert_not_called()
        options = self.factory.call_args.kwargs
        self.assertEqual((options['min_size'], options['max_size'], options['max_waiting']), (1, 4, 8))
        self.assertEqual((options['timeout'], options['max_lifetime']), (3, 1800))
        self.assertIs(options['check'], self.factory.check_connection)
        self.pool.open.assert_called_once_with(wait=False)
        self.database.close()
        self.database.close()
        self.pool.close.assert_called_once()

    def test_new_connection_parameters_are_resolved_each_time(self):
        self.parameters.side_effect = [{'password': 'FIRST'}, {'password': 'NEW'}]
        self.assertEqual(self.module.pool_parameters(), {'password': 'FIRST'})
        self.assertEqual(self.module.pool_parameters(), {'password': 'NEW'})

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
