"""No-network tests of the serving-pod concurrency evidence requirements."""
import copy
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch
import json
import io
from contextlib import redirect_stdout


spec = importlib.util.spec_from_file_location(
    'browse_concurrency', Path(__file__).resolve().parents[1] / 'scripts/check-browse-concurrency.py')
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)


class BrowseConcurrency(unittest.TestCase):
    def test_explicit_eight_slot_snapshot_preserves_legacy_default_and_bounds(self):
        row = {'instance_id': 'a' * 32, 'statistics': dict(pool_min=8, pool_max=8, pool_size=8,
            pool_available=8, requests_waiting=0, requests_num=1, requests_errors=0, connections_num=8,
            connections_errors=0, connections_lost=0, returns_bad=0, pool_waiting_limit=16)}
        call = lambda *args: row
        flow.snapshot(call, 0, expected_pool_max=8)
        with self.assertRaises(flow.CheckFailed):
            flow.snapshot(call, 0)
        for key, value in (('pool_min', 4), ('pool_max', 9), ('pool_size', 9),
                           ('pool_available', 9), ('requests_waiting', 17)):
            wrong = copy.deepcopy(row)
            wrong['statistics'][key] = value
            with self.subTest(key=key), self.assertRaises(flow.CheckFailed):
                flow.snapshot(lambda *args: wrong, 0, expected_pool_max=8)

    def scenario(self, fault=None):
        lock = threading.Lock()
        requests, snapshots = [0, 0], [0, 0]
        events = [{'id': 'demo-general', 'inventory_type': 'GENERAL'},
                  {'id': 'demo-seated', 'inventory_type': 'SEATED'}]

        def call(index, path):
            with lock:
                if path == '/internal/database-pool':
                    snapshots[index] += 1
                    changed = snapshots[index] > 1
                    return {'instance_id': ('c' if fault == 'restart' and changed else 'ab'[index]) * 32,
                            'statistics': {'pool_min': 1, 'pool_max': 4,
                                           'pool_size': 5 if fault == 'oversize' else 2,
                                           'pool_available': 2, 'requests_waiting': 0,
                                           'requests_num': requests[index],
                                           'connections_num': requests[index] if fault == 'no-reuse' else 2,
                                           'requests_errors': int(fault == 'pool-error' and requests[index] > 10),
                                           'connections_errors': 0, 'connections_lost': 0, 'returns_bad': 0}}
                if path == '/healthz':
                    return {'status': 'alive'}
                requests[index] += 1
                if path == '/readyz':
                    return {'status': 'ready'}
                if path == '/events':
                    if fault == 'http':
                        raise OSError('DO_NOT_LOG_RAW')
                    return events
                seated = 'seated' in path
                available = 99 if fault == 'changed-data' and requests[index] > 10 else 100
                return {'id': 'demo-seated' if seated else 'demo-general',
                        'inventory_type': 'SEATED' if seated else 'GENERAL', 'available': available,
                        'seats': [f'A{n}' for n in range(1, 101)] if seated else None}

        return call, requests

    def test_success_requires_both_pod_counters_and_correct_reads(self):
        call, requests = self.scenario()
        result = flow.experiment(call)
        self.assertEqual(result['result'], 'BROWSE_CONCURRENCY_OK')
        self.assertEqual(result['burst_requests'], 48)
        self.assertEqual(requests, [34, 34])
        self.assertTrue(all(row['reuse_supported'] for row in result['per_pod']))
        self.assertFalse(result['inventory_writes'])

    def test_new_connection_per_borrow_cannot_pass(self):
        call, _ = self.scenario('no-reuse')
        with self.assertRaises(ValueError):
            flow.experiment(call)

    def test_counter_error_or_wrong_data_prevents_pass(self):
        for fault in ('pool-error', 'changed-data', 'oversize', 'http', 'restart'):
            with self.subTest(fault=fault):
                call, _ = self.scenario(fault)
                with self.assertRaises((ValueError, OSError)):
                    flow.experiment(call)

    def fixtures(self):
        deployment = {'metadata': {'name': 'browse', 'namespace': 'trafficxx', 'uid': 'd', 'generation': 1},
                      'spec': {'replicas': 2},
                      'status': {'observedGeneration': 1, 'updatedReplicas': 2, 'readyReplicas': 2}}
        replica_set = {'metadata': {'uid': 'rs', 'ownerReferences': [
            {'kind': 'Deployment', 'controller': True, 'uid': 'd'}]}}
        pods = [{'metadata': {'name': name, 'uid': name, 'ownerReferences': [
                    {'kind': 'ReplicaSet', 'name': 'browse-rs', 'controller': True, 'uid': 'rs'}]},
                 'spec': {'nodeName': 'same-node'},
                 'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                for name in ('browse-a', 'browse-b')]
        return deployment, pods, replica_set

    def test_same_node_allowed_but_wrong_owner_or_unready_is_rejected(self):
        deployment, pods, replica_set = self.fixtures()
        self.assertEqual(len(flow.select_pods(deployment, pods, replica_set)), 2)
        wrong = copy.deepcopy(pods)
        wrong[0]['metadata']['ownerReferences'][0]['uid'] = 'another-rs'
        with self.assertRaises(ValueError):
            flow.select_pods(deployment, wrong, replica_set)
        pods[0]['status']['conditions'][0]['status'] = 'False'
        with self.assertRaises(ValueError):
            flow.select_pods(deployment, pods, replica_set)

    def test_booking_preflight_requires_the_requested_deployment_name(self):
        deployment, pods, replica_set = self.fixtures()
        with self.assertRaises(ValueError):
            flow.select_pods(deployment, pods, replica_set, name='booking')
        deployment['metadata']['name'] = 'booking'
        self.assertEqual(len(flow.select_pods(deployment, pods, replica_set, name='booking')), 2)
        with self.assertRaises(ValueError):
            flow.select_pods(deployment, pods, replica_set)

    def test_owned_forwards_are_stopped_on_experiment_failure(self):
        deployment, pods, replica_set = self.fixtures()
        process = MagicMock()
        process.poll.return_value = None

        def command(*args):
            if args == ('config', 'current-context'):
                return 'trafficxx-lab-aks'
            if args[1] == 'deployment':
                return json.dumps(deployment)
            if args[1] == 'pods':
                return json.dumps({'items': pods})
            return json.dumps(replica_set)

        class ForwardLog(io.BytesIO):
            def read(self, *args):
                return b'Forwarding from 127.0.0.1:18041\nForwarding from 127.0.0.1:18042'

        output = io.StringIO()
        with patch.object(flow, 'kubectl', side_effect=command), \
                patch.object(flow.subprocess, 'check_output', return_value='35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'), \
                patch.object(flow.subprocess, 'Popen', return_value=process), \
                patch.object(flow.tempfile, 'TemporaryFile', side_effect=ForwardLog), \
                patch.object(flow, 'experiment', side_effect=OSError('PRIVATE_RAW_ERROR')), \
                redirect_stdout(output):
            self.assertEqual(flow.main(), 1)
        self.assertEqual(process.terminate.call_count, 2)
        self.assertEqual(process.wait.call_count, 2)
        self.assertNotIn('PRIVATE_RAW_ERROR', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['stage'], 'read_only_experiment')
