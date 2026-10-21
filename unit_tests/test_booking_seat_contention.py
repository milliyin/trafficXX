"""Offline contention decisions and cleanup ordering, with fake HTTP responses."""
import importlib.util
from pathlib import Path
import threading
import unittest
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    'seat_probe', Path(__file__).resolve().parents[1] / 'scripts/check-booking-seat-contention.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class SeatContention(unittest.TestCase):
    def scenario(self, mode='success'):
        lock = threading.Lock()
        state = {'winner': None, 'rows': {}, 'cancelled': set(), 'calls': []}

        def call(index, method, path, customer, key=None, body=None):
            with lock:
                state['calls'].append((index, method, path, customer, key, body))
                if path == '/reservations':
                    self.assertEqual(body, {'event_id': 'demo-seated', 'seats': ['A1']})
                    if mode == 'both-unavailable':
                        return 409, {'detail': {'code': 'SEAT_UNAVAILABLE'}}
                    if state['winner'] is None or mode == 'two-winners':
                        state['winner'] = index
                        row = {'id': str(uuid4()), 'event_id': 'demo-seated', 'quantity': 1,
                               'seats': ['A1'], 'status': 'HELD', 'expires_at': 'same'}
                        state['rows'][index] = row
                        if mode == 'lost-winner-response':
                            raise OSError('Committed, response lost')
                        return 201, dict(row)
                    if index in state['rows']:
                        return 200, dict(state['rows'][index])
                    code = 'OTHER_CONFLICT' if mode == 'wrong-conflict' else 'SEAT_UNAVAILABLE'
                    return 409, {'detail': {'code': code}}
                reservation_id = path.split('/')[2]
                row = next(r for r in state['rows'].values() if r['id'] == reservation_id)
                if path.endswith('/cancel'):
                    if mode == 'cancel-fails':
                        return 503, {}
                    state['cancelled'].add(reservation_id)
                return 200, {**row, 'status': 'CANCELLED' if reservation_id in state['cancelled'] else 'HELD'}
        return call, state

    def test_distinct_customers_and_keys_one_winner_cancelled(self):
        call, state = self.scenario()
        result = flow.experiment(call, 'customer', 'key')
        self.assertEqual(result['result'], 'BOOKING_SEAT_CONTENTION_OK')
        creates = [c for c in state['calls'] if c[2] == '/reservations']
        self.assertEqual({c[3] for c in creates}, {'customer-seat-0', 'customer-seat-1'})
        self.assertEqual({c[4] for c in creates}, {'key-seat-0', 'key-seat-1'})
        self.assertEqual(state['cancelled'], {result['reservation_id']})

    def test_two_winners_fail_and_both_holds_are_cancelled(self):
        call, state = self.scenario('two-winners')
        with self.assertRaises(ValueError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(state['cancelled'], {r['id'] for r in state['rows'].values()})
        self.assertEqual(len(state['cancelled']), 2)

    def test_already_occupied_seat_fails_without_canceling_someone_else(self):
        call, state = self.scenario('both-unavailable')
        with self.assertRaises(ValueError):
            flow.experiment(call, 'customer', 'key')
        self.assertFalse(state['cancelled'])

    def test_unknown_winner_is_resolved_before_any_cancel(self):
        call, state = self.scenario('lost-winner-response')
        with self.assertRaises(OSError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(len(state['cancelled']), 1)
        paths = [c[2] for c in state['calls']]
        self.assertEqual(paths[:3], ['/reservations'] * 3)
        self.assertTrue(paths[3].endswith('/cancel'))

    def test_cleanup_failure_prevents_success(self):
        call, _ = self.scenario('cancel-fails')
        with self.assertRaisesRegex(ValueError, 'Unresolved request or cancellation'):
            flow.experiment(call, 'customer', 'key')

    def test_other_conflict_is_not_a_seat_contention_pass(self):
        call, state = self.scenario('wrong-conflict')
        with self.assertRaises(ValueError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(len(state['cancelled']), 1)
