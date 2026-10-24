"""Offline restoration and evidence gates for the controlled Booking drill."""
from copy import deepcopy
from datetime import datetime, timezone
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('booking_drill', ROOT / 'scripts/check-booking-alert-recovery.py')
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def deployment():
    return {'metadata': {'uid': 'booking-deployment', 'resourceVersion': '17', 'generation': 1},
            'spec': {'replicas': 2, 'template': {'spec': {}}},
            'status': {'observedGeneration': 1, 'replicas': 2, 'updatedReplicas': 2,
                       'readyReplicas': 2, 'availableReplicas': 2}}


class BookingAlertRecovery(unittest.TestCase):
    def exercise(self, failure=None, restore_failure=False, scale_failure=None):
        initial = deployment()
        survivor = {'pod': 'booking-survivor', 'uid': 'survivor', 'restarts': 0}
        replacement = {'pod': 'booking-new', 'uid': 'new', 'restarts': 0}
        old = {'pod': 'booking-old', 'uid': 'old', 'restarts': 0}
        two = iter([[survivor, old], [survivor, replacement]])
        alerts = SimpleNamespace(verify=Mock(return_value={'target_count': 2, 'rule_count': 5}))
        scale = Mock(side_effect=scale_failure)
        restored = Mock(side_effect=RuntimeError() if restore_failure else None)
        firing = Mock(side_effect=failure)
        if failure is None:
            firing.return_value = {'state': 'firing'}
        output = io.StringIO()
        result, caught = None, None
        with patch.object(drill, 'deployment', return_value=initial), \
                patch.object(drill, 'kube', return_value=json.dumps({'items': []})), \
                patch.object(drill, 'load', return_value=alerts), \
                patch.object(drill, 'stable', side_effect=lambda initial, count:
                             next(two) if count == 2 else [survivor]), \
                patch.object(drill, 'wait', side_effect=lambda check, seconds: check()), \
                patch.object(drill, 'scale_down', scale), patch.object(drill, 'restore', restored), \
                patch.object(drill, 'firing', firing), patch.object(drill, 'history') as history, \
                contextlib.redirect_stdout(output):
            try:
                result = drill.run(lambda path: {}, observe_seconds=0)
            except BaseException as error:
                caught = error
        return result, caught, restored, history, output.getvalue()

    def test_success_requires_restore_then_healthy_rules_and_stored_history(self):
        result, error, restored, history, output = self.exercise()
        self.assertIsNone(error)
        self.assertEqual(result['result'], 'BOOKING_ALERT_RECOVERY_OK')
        self.assertEqual(result['ready_replicas'], 2)
        self.assertFalse(result['booking_http_continuity_tested'])
        self.assertFalse(result['inventory_writes'])
        restored.assert_called_once()
        history.assert_called_once()
        self.assertIn('BOOKING_REPLICAS_RESTORED', output)

    def test_failure_and_ctrl_c_restore_without_success(self):
        for failure in (RuntimeError('PRIVATE'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                result, error, restored, history, output = self.exercise(failure)
                self.assertIsNone(result)
                self.assertIs(error, failure)
                restored.assert_called_once()
                history.assert_not_called()
                self.assertNotIn('PRIVATE', output)

    def test_restoration_failure_cannot_report_pass(self):
        result, error, restored, history, output = self.exercise(restore_failure=True)
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn('BOOKING_RESTORATION_INCOMPLETE', output)
        history.assert_not_called()

    def test_lost_scale_response_still_attempts_owned_restoration(self):
        failure = subprocess.TimeoutExpired('kubectl', 30)
        result, error, restored, history, output = self.exercise(scale_failure=failure)
        self.assertIsNone(result)
        self.assertIs(error, failure)
        restored.assert_called_once()
        history.assert_not_called()

    def test_existing_marker_or_hpa_prevents_scale_down(self):
        for mode in ('marker', 'hpa'):
            resource = deployment()
            if mode == 'marker':
                resource['metadata']['annotations'] = {drill.MARKER: 'another-run'}
            hpas = [{'spec': {'scaleTargetRef': {'kind': 'Deployment', 'name': 'booking'}}}] if mode == 'hpa' else []
            with patch.object(drill, 'deployment', return_value=resource), \
                    patch.object(drill, 'kube', return_value=json.dumps({'items': hpas})), \
                    patch.object(drill, 'scale_down') as scale, \
                    self.subTest(mode=mode), self.assertRaises(drill.CheckFailed):
                drill.run(lambda path: {}, 0)
            scale.assert_not_called()

    def test_scale_down_preserves_annotations_and_has_atomic_preconditions(self):
        initial = deployment()
        initial['metadata']['annotations'] = {'existing': 'keep'}
        with patch.object(drill, 'kube', return_value='{}') as kube:
            drill.scale_down(initial, 'this-run')
        args = kube.call_args.args
        operations = json.loads(args[args.index('--patch') + 1])
        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'booking-deployment'})
        self.assertEqual(operations[1]['path'], '/metadata/resourceVersion')
        self.assertEqual(operations[2], {'op': 'test', 'path': '/spec/replicas', 'value': 2})
        self.assertEqual(operations[3]['value'], {'existing': 'keep', drill.MARKER: 'this-run'})
        self.assertEqual(operations[4]['value'], 1)

    def test_restore_requires_own_marker_uid_and_expected_replica_count(self):
        for mode in ('uid', 'foreign-marker', 'replicas', 'missing-marker-one'):
            resource = deployment()
            resource['metadata']['annotations'] = {drill.MARKER: 'this-run'}
            resource['spec']['replicas'] = 1
            if mode == 'uid':
                resource['metadata']['uid'] = 'replacement-deployment'
            elif mode == 'foreign-marker':
                resource['metadata']['annotations'][drill.MARKER] = 'another-run'
            elif mode == 'replicas':
                resource['spec']['replicas'] = 3
            else:
                resource['metadata']['annotations'] = {}
            with patch.object(drill, 'deployment', return_value=resource), \
                    patch.object(drill, 'patch') as mutate, \
                    self.subTest(mode=mode), self.assertRaises(drill.CheckFailed):
                drill.restore(deployment(), 'this-run')
            mutate.assert_not_called()

    def test_restore_after_lost_response_and_resource_version_conflict(self):
        resource = deployment()
        resource['metadata']['annotations'] = {drill.MARKER: 'this-run'}
        resource['spec']['replicas'] = 1
        with patch.object(drill, 'deployment', return_value=resource), \
                patch.object(drill, 'patch', side_effect=[subprocess.TimeoutExpired('kubectl', 30), {}]) as mutate, \
                patch.object(drill.time, 'sleep'):
            self.assertTrue(drill.restore(deployment(), 'this-run'))
        self.assertEqual(mutate.call_count, 2)
        self.assertEqual(mutate.call_args.args[1][-1]['value'], 2)

    def test_fatal_changed_survivor_is_not_retried(self):
        check = Mock(side_effect=drill.CheckFailed('SURVIVING_PROCESS_CHANGED'))
        sleeper = Mock()
        with self.assertRaises(drill.CheckFailed):
            drill.wait(check, 30, sleep=sleeper, clock=lambda: 0)
        sleeper.assert_not_called()

    def test_history_requires_firing_sample_from_this_run(self):
        for stamp in (99, 101):
            query = Mock(return_value={'status': 'success', 'data': {'resultType': 'matrix',
                         'result': [{'values': [[stamp, '1']]}]}})
            if stamp < 100:
                with self.assertRaises(drill.CheckFailed):
                    drill.history(query, 100)
            else:
                drill.history(query, 100)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(query.call_args.args[0]).query)
            self.assertEqual(params['start'], ['100'])
            self.assertIn('alertstate="firing"', params['query'][0])

    def test_firing_requires_healthy_rules_and_native_firing_series(self):
        alerts = drill.load('check-browse-alerts')
        rows = [{'name': name, 'type': 'alerting', 'duration': duration, 'query': expression,
                 'health': 'ok', 'lastError': '',
                 'lastEvaluation': datetime.now(timezone.utc).isoformat(),
                 'labels': {'component': 'booking-pool', 'severity': 'warning'},
                 'state': 'firing' if name == 'TrafficxxBookingTargetsUnavailable' else 'inactive'}
                for name, (duration, expression) in alerts.rules_for('booking').items()]

        def get(path):
            if path == '/api/v1/rules':
                return {'data': {'groups': [{'name': 'trafficxx-booking-pool', 'interval': 15, 'rules': rows}]}}
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['query'][0]
            if path.startswith('/api/v1/format_query?'):
                return {'status': 'success', 'data': query}
            return {'status': 'success', 'data': {'resultType': 'vector',
                    'result': [{'value': [0, '1']}]}}
        self.assertEqual(drill.firing(get, alerts)['state'], 'firing')
        with patch.object(drill, 'scalar', return_value=0), self.assertRaises(drill.Retry):
            drill.firing(get, alerts)
        for fault in ('health', 'duration', 'state'):
            changed = deepcopy(rows)
            changed[1][fault] = {'health': 'err', 'duration': 0, 'state': 'firing'}[fault]
            saved, rows = rows, changed
            with self.subTest(fault=fault), self.assertRaises(drill.CheckFailed):
                drill.firing(get, alerts)
            rows = saved

    def test_stable_snapshot_requires_owned_ready_pods_and_same_template(self):
        initial = deployment()
        pod = {'metadata': {'name': 'booking-a', 'uid': 'pod-a', 'ownerReferences': [
                    {'controller': True, 'kind': 'ReplicaSet', 'uid': 'rs'}]},
               'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                          'containerStatuses': [{'name': 'booking', 'ready': True, 'restartCount': 0}]}}
        replica_set = {'metadata': {'uid': 'rs', 'ownerReferences': [
            {'controller': True, 'kind': 'Deployment', 'uid': initial['metadata']['uid']}]}}
        pods = [pod, deepcopy(pod)]
        pods[1]['metadata']['uid'] = 'pod-b'

        def kube(*args):
            return json.dumps({'items': [replica_set] if args[1] == 'replicasets' else pods})
        with patch.object(drill, 'deployment', return_value=deepcopy(initial)) as current, \
                patch.object(drill, 'kube', side_effect=kube):
            self.assertEqual(len(drill.stable(initial, 2)), 2)
            pod['metadata']['ownerReferences'][0]['uid'] = 'foreign-rs'
            with self.assertRaises(drill.CheckFailed):
                drill.stable(initial, 2)
            pod['metadata']['ownerReferences'][0]['uid'] = 'rs'
            current.return_value['spec']['template']['spec']['changed'] = True
            with self.assertRaises(drill.CheckFailed):
                drill.stable(initial, 2)
