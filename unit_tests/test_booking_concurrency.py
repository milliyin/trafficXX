"""Offline coordination, failure recovery and pod-ownership checks."""
import copy
import importlib.util
from pathlib import Path
import threading
import unittest
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    'booking_concurrency', Path(__file__).resolve().parents[1] /
    'scripts/check-booking-concurrency.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class ConcurrentBooking(unittest.TestCase):
    def scenario(self, mode='success'):
        lock = threading.Lock()
        held = {'id': str(uuid4()), 'event_id': 'demo-general', 'quantity': 2,
                'status': 'HELD', 'expires_at': 'original'}
        state = {'creates': 0, 'cancelled': set(), 'keys': [], 'indices': set()}

        def call(index, method, path, customer, key=None, body=None):
            with lock:
                if path == '/reservations':
                    state['creates'] += 1
                    number = state['creates']
                    state['keys'].append((customer, key))
                    state['indices'].add(index)
                    if mode == 'lost-responses' and number <= 2:
                        raise OSError('Response lost after commit')
                    row = dict(held)
                    if mode == 'changed-expiry' and number == 2:
                        row['expires_at'] = 'extended'
                    if mode == 'two-reservations' and number == 2:
                        row['id'] = str(uuid4())
                        state['second_id'] = row['id']
                    return (201 if number == 1 or mode == 'two-reservations' else 200), row
                reservation_id = path.split('/')[2]
                if path.endswith('/cancel'):
                    if mode == 'cancel-fails':
                        return 503, {}
                    state['cancelled'].add(reservation_id)
                row = {**held, 'id': reservation_id}
                if reservation_id in state['cancelled']:
                    row['status'] = 'CANCELLED'
                return 200, row
        return call, state, held

    def test_two_endpoints_share_one_key_and_cleanup_before_success(self):
        call, state, held = self.scenario()
        result = flow.experiment(call, 'customer', 'key')
        self.assertEqual(result['http_statuses'], [200, 201])
        self.assertEqual(state['indices'], {0, 1})
        self.assertEqual(set(state['keys']), {('customer', 'key')})
        self.assertEqual(state['cancelled'], {held['id']})

    def test_changed_expiry_is_rejected_and_original_cancelled(self):
        call, state, held = self.scenario('changed-expiry')
        with self.assertRaises(ValueError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(state['cancelled'], {held['id']})

    def test_two_reservations_are_rejected_and_both_cancelled(self):
        call, state, held = self.scenario('two-reservations')
        with self.assertRaises(ValueError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(state['cancelled'], {held['id'], state['second_id']})

    def test_lost_responses_recover_with_original_key_then_fail_test(self):
        call, state, held = self.scenario('lost-responses')
        with self.assertRaises(OSError):
            flow.experiment(call, 'customer', 'key')
        self.assertEqual(state['creates'], 3)
        self.assertEqual(set(state['keys']), {('customer', 'key')})
        self.assertEqual(state['cancelled'], {held['id']})

    def test_cancellation_failure_prevents_success(self):
        call, _, _ = self.scenario('cancel-fails')
        with self.assertRaisesRegex(ValueError, 'Cancellation not verified'):
            flow.experiment(call, 'customer', 'key')

    def fixtures(self):
        deployment = {'spec': {'replicas': 2}, 'metadata': {'generation': 1, 'uid': 'deploy'},
                      'status': {'observedGeneration': 1, 'readyReplicas': 2, 'updatedReplicas': 2}}
        rs = {'metadata': {'uid': 'rs', 'ownerReferences': [
            {'controller': True, 'kind': 'Deployment', 'uid': 'deploy'}]}}
        pod = {'metadata': {'name': 'one', 'ownerReferences': [
            {'controller': True, 'kind': 'ReplicaSet', 'uid': 'rs'}]},
               'spec': {'nodeName': 'node1'}, 'status': {'phase': 'Running', 'conditions': [
                   {'type': 'Ready', 'status': 'True'}]}}
        second = copy.deepcopy(pod)
        second['metadata']['name'] = 'two'
        second['spec']['nodeName'] = 'node2'
        return deployment, [pod, second], rs

    def test_two_owned_ready_pods_are_selected(self):
        self.assertEqual(flow.select_pods(*self.fixtures()), ['one', 'two'])

    def test_wrong_controller_and_same_node_are_rejected(self):
        for mutation in ('owner', 'node', 'terminating', 'rollout'):
            with self.subTest(mutation=mutation):
                deployment, pods, rs = self.fixtures()
                if mutation == 'owner':
                    rs['metadata']['ownerReferences'][0]['uid'] = 'other'
                elif mutation == 'node':
                    pods[1]['spec']['nodeName'] = 'node1'
                elif mutation == 'terminating':
                    pods[0]['metadata']['deletionTimestamp'] = 'now'
                else:
                    deployment['status']['updatedReplicas'] = 1
                with self.assertRaises(ValueError):
                    flow.select_pods(deployment, pods, rs)
