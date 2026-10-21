"""Offline response validation for the Cloud Shell HTTP helper."""
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'scripts/check-browse-http.sh').read_text()
code = compile(source.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0], '<browse-http-helper>', 'exec')


class DemoHttp(unittest.TestCase):
    def responses(self):
        return {
            '/healthz': {'status': 'alive'}, '/readyz': {'status': 'ready'},
            '/events': [{'id': 'demo-general', 'inventory_type': 'GENERAL'},
                        {'id': 'demo-seated', 'inventory_type': 'SEATED'}],
            '/events/demo-general/availability': {
                'id': 'demo-general', 'inventory_type': 'GENERAL', 'available': 100, 'seats': None},
            '/events/demo-seated/availability': {
                'id': 'demo-seated', 'inventory_type': 'SEATED', 'available': 100,
                'seats': [f'A{n}' for n in range(1, 101)]},
        }

    def check(self, responses, mode='--demo'):
        opener = MagicMock()
        opener.open.side_effect = lambda url, timeout: io.BytesIO(
            json.dumps(responses[url.removeprefix('http://127.0.0.1:18001')]).encode())
        output, status = io.StringIO(), 0
        with patch('urllib.request.build_opener', return_value=opener), \
                patch('sys.argv', ['check', mode]), contextlib.redirect_stdout(output):
            try:
                exec(code, {'__name__': '__main__'})
            except SystemExit as error:
                status = error.code
        return status, json.loads(output.getvalue()), opener.open.call_count

    def test_two_full_demo_events_pass(self):
        status, result, calls = self.check(self.responses())
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'BROWSE_DEMO_HTTP_OK')
        self.assertEqual(result['demo_availability'], {'demo-general': 100, 'demo-seated': 100})
        self.assertEqual(calls, 5)

    def test_legitimately_reserved_seats_and_tickets_can_still_pass(self):
        responses = self.responses()
        responses['/events/demo-general/availability']['available'] = 90
        responses['/events/demo-seated/availability'].update(
            available=98, seats=[f'A{n}' for n in range(3, 101)])
        self.assertEqual(self.check(responses)[0], 0)

    def test_duplicate_or_invalid_seats_and_out_of_range_counts_fail(self):
        for change in ({'seats': ['A1'] * 100}, {'available': 101}, {'available': -1},
                       {'seats': [f'B{n}' for n in range(1, 101)]}):
            with self.subTest(change=change):
                responses = self.responses()
                responses['/events/demo-seated/availability'].update(change)
                status, result, _ = self.check(responses)
                self.assertEqual(status, 1)
                self.assertEqual(result['result'], 'HTTP_CHECK_FAILED')

    def test_missing_demo_event_fails(self):
        responses = self.responses()
        responses['/events'] = []
        self.assertEqual(self.check(responses)[0], 1)

    def test_default_http_smoke_mode_remains_compatible(self):
        status, result, calls = self.check(self.responses(), mode='')
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'BROWSE_HTTP_OK')
        self.assertEqual(calls, 3)


if __name__ == '__main__':
    unittest.main()
