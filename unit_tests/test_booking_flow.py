"""Offline failure-path tests for the user-run HTTP booking experiment."""
import importlib.util
from pathlib import Path
import unittest
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    'booking_flow', Path(__file__).resolve().parents[1] / 'scripts/check-booking-flow.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class BookingFlow(unittest.TestCase):
    def scenario(self, mode='success'):
        calls = []
        held = {'id': str(uuid4()), 'status': 'HELD', 'quantity': 2,
                'event_id': 'demo-general', 'expires_at': 'unchanged'}
        cancelled = {**held, 'status': 'CANCELLED'}

        def call(method, path, customer, key=None, body=None):
            calls.append((method, path, customer, key, body))
            if mode == 'create-fails' and len(calls) == 1:
                raise OSError('Network result unknown')
            if path.endswith('/cancel'):
                if mode == 'cleanup-fails':
                    return 503, {}
                return 200, dict(cancelled)
            if method == 'GET':
                return 404, {'detail': {'code': 'RESERVATION_NOT_FOUND'}}
            if body['quantity'] == 3:
                return 409, {'detail': {'code': 'IDEMPOTENCY_KEY_REUSED'}}
            if len(calls) == 1:
                return 201, dict(held)
            if len(calls) == 2:
                result = dict(held)
                if mode in ('bad-retry', 'cleanup-fails'):
                    result['expires_at'] = 'extended'
                return 200, result
            if mode == 'recreated-after-cancel':
                return 201, dict(held)
            return 200, dict(cancelled)

        return call, calls

    def test_success_reuses_customer_key_and_leaves_cancelled_history(self):
        call, calls = self.scenario()
        result = flow.run(call)
        self.assertEqual(result['result'], 'BOOKING_FLOW_OK')
        self.assertEqual(len(calls), 7)
        creates = [c for c in calls if c[1] == '/reservations']
        self.assertEqual(len({(c[2], c[3]) for c in creates}), 1)
        self.assertEqual(calls[-1][4]['quantity'], 2)

    def test_changed_expiry_fails_and_cancels_original_hold(self):
        call, calls = self.scenario('bad-retry')
        with self.assertRaises(AssertionError):
            flow.run(call)
        self.assertTrue(calls[-1][1].endswith('/cancel'))

    def test_recreated_cancelled_key_fails_and_attempts_cleanup(self):
        call, calls = self.scenario('recreated-after-cancel')
        with self.assertRaises(AssertionError):
            flow.run(call)
        self.assertTrue(calls[-1][1].endswith('/cancel'))

    def test_unknown_create_outcome_does_not_submit_new_key(self):
        call, calls = self.scenario('create-fails')
        with self.assertRaises(OSError):
            flow.run(call)
        self.assertEqual(len(calls), 1)

    def test_failed_cleanup_is_reported(self):
        call, _ = self.scenario('cleanup-fails')
        with self.assertRaisesRegex(RuntimeError, 'Cleanup not verified'):
            flow.run(call)
