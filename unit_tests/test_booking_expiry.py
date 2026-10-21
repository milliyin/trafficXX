"""Offline expiry decisions and failure cleanup; simulated clock, no real wait."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    'expiry_probe', Path(__file__).resolve().parents[1] / 'scripts/check-booking-expiry.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class ExpiryProbe(unittest.TestCase):
    def scenario(self, mode='success'):
        now = [0]
        rows, keys, cancelled = {}, [], set()

        def sleep(seconds):
            now[0] += seconds

        def call(index, method, path, customer, key=None, body=None):
            if now[0] >= 305 and 0 in rows and mode != 'old-still-held':
                rows[0]['status'] = 'EXPIRED'
            if path == '/reservations':
                keys.append((index, customer, key))
                if index == 1 and (now[0] < 305 or mode == 'not-reusable'):
                    return 409, {'detail': {'code': 'SEAT_UNAVAILABLE'}}
                if index in rows:
                    return 200, dict(rows[index])
                rows[index] = {'id': str(uuid4()), 'event_id': 'demo-seated', 'seats': ['A1'],
                               'quantity': 1, 'status': 'HELD',
                               'created_at': '2026-10-22T00:00:00+00:00',
                               'expires_at': '2026-10-22T00:05:00+00:00'}
                if mode == 'wrong-ttl':
                    rows[index]['expires_at'] = '2026-10-22T00:01:00+00:00'
                return 201, dict(rows[index])
            reservation_id = path.split('/')[2]
            row = next(r for r in rows.values() if r['id'] == reservation_id)
            if path.endswith('/cancel'):
                if mode == 'cleanup-fails':
                    return 503, {}
                row['status'] = 'CANCELLED'
                cancelled.add(reservation_id)
            return 200, dict(row)
        return call, now, sleep, rows, keys, cancelled

    def run_scenario(self, mode='success'):
        call, now, sleep, rows, keys, cancelled = self.scenario(mode)
        with patch.object(flow.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(flow.time, 'sleep', side_effect=sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            result = flow.experiment(call, 'customer', 'key')
        return result, now, rows, keys, cancelled

    def test_real_duration_path_reuses_failed_key_and_cancels_replacement(self):
        result, now, rows, keys, cancelled = self.run_scenario()
        self.assertEqual(result['result'], 'BOOKING_EXPIRY_OK')
        self.assertEqual(now[0], 305)
        self.assertEqual(rows[0]['status'], 'EXPIRED')
        self.assertEqual(rows[1]['status'], 'CANCELLED')
        self.assertEqual(cancelled, {rows[1]['id']})
        self.assertEqual({(c, k) for i, c, k in keys if i == 1},
                         {('customer-expiry-1', 'key-expiry-1')})

    def test_non_five_minute_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'five-minute'):
            self.run_scenario('wrong-ttl')

    def test_seat_not_reusable_after_wait_is_failure(self):
        with self.assertRaises(ValueError):
            self.run_scenario('not-reusable')

    def test_old_hold_must_be_expired(self):
        with self.assertRaisesRegex(ValueError, 'not EXPIRED'):
            self.run_scenario('old-still-held')

    def test_cleanup_failure_prevents_success(self):
        with self.assertRaisesRegex(ValueError, 'cleanup failure'):
            self.run_scenario('cleanup-fails')
