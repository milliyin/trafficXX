"""Operations readiness must not require booking-table access."""
import importlib.util
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


class SqlError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class App:
    def __init__(self):
        self.routes = {}

    def get(self, path):
        def route(function):
            self.routes[path] = function
            return function
        return route


class HealthReadiness(unittest.TestCase):
    def scenario(self, query=None, error=False):
        api, driver, database = ModuleType('fastapi'), ModuleType('psycopg'), ModuleType('trafficxx.db')
        api.HTTPException, driver.Error = HttpError, SqlError
        conn = MagicMock()
        conn.__enter__.return_value = conn
        if error:
            conn.execute.side_effect = SqlError('DO_NOT_LOG_RAW')
        database.connect = MagicMock(return_value=conn)
        spec = importlib.util.spec_from_file_location(
            'health_check', Path(__file__).resolve().parents[1] / 'trafficxx/health.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'fastapi': api, 'psycopg': driver, 'trafficxx.db': database}):
            spec.loader.exec_module(module)
            app = App()
            module.register(app, **({'readiness_query': query} if query is not None else {}))
        return app, conn

    def test_operations_select_one_needs_no_event_privilege(self):
        app, conn = self.scenario('SELECT 1')
        self.assertEqual(app.routes['/readyz'](), {'status': 'ready'})
        conn.execute.assert_called_once_with('SELECT 1')

    def test_existing_api_default_keeps_event_readiness_query(self):
        app, conn = self.scenario()
        self.assertEqual(app.routes['/readyz'](), {'status': 'ready'})
        conn.execute.assert_called_once_with('SELECT 1 FROM events LIMIT 1')

    def test_failure_is_sanitized_and_liveness_does_not_query_database(self):
        app, conn = self.scenario('SELECT 1', error=True)
        self.assertEqual(app.routes['/healthz'](), {'status': 'alive'})
        conn.execute.assert_not_called()
        with self.assertRaises(HttpError) as caught:
            app.routes['/readyz']()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, {'code': 'DATABASE_UNAVAILABLE'})
        self.assertNotIn('DO_NOT_LOG_RAW', str(caught.exception.detail))
