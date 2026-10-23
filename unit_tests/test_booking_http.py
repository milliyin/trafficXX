"""Offline read-only HTTP smoke response checks for the real Booking API."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'scripts/check-booking-http.sh').read_text()
code = compile(source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], '<booking-http>', 'exec')


class BookingHttp(unittest.TestCase):
    def check(self, mode='success', pool_mode=False):
        opener = MagicMock()
        methods = []
        snapshots = []

        def open_response(request, timeout):
            if isinstance(request, urllib.request.Request):
                methods.append(request.get_method())
                self.assertIsNone(request.data)
                self.assertIn(('X-customer-id', 'lab-readonly-smoke'), request.header_items())
                if mode == 'unexpected-reservation':
                    return io.BytesIO(b'{}')
                payload = {'detail': {'code': 'RESERVATION_NOT_FOUND'}}
                if mode == 'wrong-404':
                    payload = {'detail': 'Not Found'}
                raise urllib.error.HTTPError(request.full_url, 404, 'Not Found', {},
                                             io.BytesIO(json.dumps(payload).encode()))
            methods.append('GET')
            path = request.removeprefix('http://127.0.0.1:18002')
            if path == '/healthz':
                result = {'status': 'alive'}
            elif path == '/readyz':
                if mode == 'not-ready':
                    raise urllib.error.HTTPError(request, 503, 'Unavailable', {}, io.BytesIO(b'{}'))
                result = {'status': 'ready'}
            elif path == '/internal/database-pool':
                snapshots.append(1)
                after = len(snapshots) == 2
                result = {'instance_id': 'process', 'statistics': {
                    'requests_num': 13 if after else 10, 'connections_num': 1,
                    'pool_max': 4, 'pool_size': 1, 'requests_errors': 0,
                    'connections_errors': 0, 'connections_lost': 0, 'returns_bad': 0}}
                if after and mode == 'pool-reset':
                    result['instance_id'] = 'replacement-process'
                if after and mode == 'no-reuse':
                    result['statistics']['connections_num'] = 4
                if after and mode == 'pool-errors':
                    result['statistics']['requests_errors'] = 1
            else:
                self.assertEqual(path, '/openapi.json')
                result = {'info': {'title': 'trafficXX Booking API'}, 'paths': {'/reservations': {'post': {}}}}
                if mode == 'wrong-api':
                    result['info']['title'] = 'trafficXX Browse API'
                if mode == 'missing-route':
                    result['paths'] = {}
            return io.BytesIO(json.dumps(result).encode())

        opener.open.side_effect = open_response
        output, status = io.StringIO(), 0
        with patch('urllib.request.build_opener', return_value=opener), contextlib.redirect_stdout(output), \
                patch.dict(os.environ, {'BOOKING_CHECK_POOL': '--pool' if pool_mode else ''}):
            try:
                exec(code, {'__name__': '__main__'})
            except SystemExit as error:
                status = error.code
        return status, json.loads(output.getvalue()), methods

    def test_success_uses_gets_only_and_confirms_database_backed_404(self):
        status, result, methods = self.check()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'BOOKING_HTTP_OK')
        self.assertEqual(result['missing_reservation_status'], 404)
        self.assertEqual(methods, ['GET'] * 4)

    def test_wrong_api_module_is_rejected(self):
        self.assertEqual(self.check('wrong-api')[0], 1)

    def test_missing_post_route_is_rejected(self):
        self.assertEqual(self.check('missing-route')[0], 1)

    def test_unexpected_existing_reservation_is_rejected(self):
        self.assertEqual(self.check('unexpected-reservation')[0], 1)

    def test_generic_router_404_is_not_database_lookup_proof(self):
        self.assertEqual(self.check('wrong-404')[0], 1)

    def test_readiness_failure_is_reported_without_server_body(self):
        status, result, _ = self.check('not-ready')
        self.assertEqual(status, 1)
        self.assertEqual(result['http_status'], 503)
        self.assertEqual(result['result'], 'HTTP_CHECK_FAILED')

    def test_optional_pool_check_requires_reuse_and_readiness_after_rollback(self):
        status, result, methods = self.check(pool_mode=True)
        self.assertEqual(status, 0)
        self.assertEqual(result['pool']['pool_borrows_delta'], 3)
        self.assertTrue(result['pool']['readiness_after_lookup_rollback'])
        self.assertEqual(methods, ['GET'] * 7)
        self.assertFalse(result['inventory_writes'])

    def test_process_reset_direct_connections_or_new_errors_cannot_pass_reuse(self):
        for fault in ('pool-reset', 'no-reuse', 'pool-errors'):
            with self.subTest(fault=fault):
                self.assertEqual(self.check(fault, pool_mode=True)[0], 1)


if __name__ == '__main__':
    unittest.main()
