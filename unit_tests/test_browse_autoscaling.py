"""Policy bounds, missing-data handling and real-controller evidence gates."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scaling = load('scaling', 'scripts/browse-autoscaling.py')
read = load('scaling_read', 'scripts/check-read-load.py')
with patch.dict(sys.modules, {'read_probe': read}):
    probe = load('scaling_probe', 'scripts/check-browse-autoscaling-load.py')


def app():
    return {'metadata': {'uid': 'owned'}, 'spec': {'replicas': 2, 'strategy': {
        'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1}}, 'template': {'spec': {'containers': [{
            'name': 'browse', 'env': [{'name': 'BROWSE_DB_POOL_MIN', 'value': '8'},
                {'name': 'BROWSE_DB_POOL_MAX', 'value': '8'},
                {'name': 'BROWSE_DB_POOL_MAX_WAITING', 'value': '16'}]}]}}},
        'status': {'readyReplicas': 2}}


class Scaling(unittest.TestCase):
    def test_policy_is_bounded_and_missing_data_is_not_zero(self):
        row = scaling.scaled_object()['spec']
        self.assertEqual((row['minReplicaCount'], row['maxReplicaCount']), (2, 4))
        self.assertEqual(row['advanced']['horizontalPodAutoscalerConfig']['behavior']['scaleDown']
                         ['stabilizationWindowSeconds'], 300)
        self.assertEqual([t['metadata']['threshold'] for t in row['triggers']], ['5', '40'])
        for trigger in row['triggers']:
            self.assertEqual(trigger['metricType'], 'AverageValue')
            self.assertEqual(trigger['metadata']['ignoreNullValues'], 'false')
            self.assertNotIn('or vector(0)', trigger['metadata']['query'])
            self.assertIn('timestamp(', trigger['metadata']['query'])
            self.assertIn('count(up', trigger['metadata']['query'])

    def test_promtool_scenarios_use_exact_deployed_queries(self):
        rows = json.loads((ROOT / 'monitoring/prometheus/browse-autoscaling.test.json').read_text())
        self.assertEqual({t['name'] for t in rows['tests']}, {'healthy', 'missing', 'down', 'stale'})
        for test in rows['tests']:
            self.assertEqual([r['expr'] for r in test['promql_expr_test']], [scaling.PRESSURE, scaling.BORROWS])

    def test_api_defaulting_cannot_change_reviewed_values(self):
        reviewed = scaling.scaled_object()['spec']
        actual = dict(deepcopy(reviewed), cooldownPeriod=300)
        self.assertTrue(scaling.contains(actual, reviewed))
        actual['maxReplicaCount'] = 5
        self.assertFalse(scaling.contains(actual, reviewed))
        actual = deepcopy(reviewed)
        actual['triggers'].append(actual['triggers'][0])
        self.assertFalse(scaling.contains(actual, reviewed))

    def test_deployment_rejects_pool_budget_and_identity_changes(self):
        with patch.object(scaling.trial, 'deployment', return_value=app()):
            self.assertEqual(scaling.deployment('owned')['spec']['replicas'], 2)
            with self.assertRaisesRegex(ValueError, 'DEPLOYMENT_REPLACED'):
                scaling.deployment('foreign')
        for mutate, code in ((lambda a: a['spec'].update(replicas=5), 'REPLICA_BUDGET'),
                (lambda a: a['spec']['template']['spec']['containers'][0]['env'][0].update(value='4'), 'POOL_POLICY'),
                (lambda a: a['spec']['strategy']['rollingUpdate'].update(maxSurge=1), 'ROLLOUT_BUDGET')):
            row = app()
            mutate(row)
            with patch.object(scaling.trial, 'deployment', return_value=row), self.assertRaisesRegex(ValueError, code):
                scaling.deployment()

    def test_missing_or_nonfinite_pressure_cannot_pass(self):
        for rows in ([], [{'value': [0, 'NaN']}], [{'value': [0, '-1']}],
                     [{'value': [0, '1']}, {'value': [0, '2']}]):
            with self.assertRaises(ValueError):
                scaling.scalar(lambda _: rows, 'query')

    def test_natural_downscale_requires_two_ready_pods(self):
        row = app()
        row['spec']['replicas'] = 3
        with patch.object(scaling, 'deployment', return_value=row), patch.object(scaling, 'check') as check:
            with self.assertRaisesRegex(ValueError, 'NATURAL_DOWNSCALE'):
                scaling.idle_two('owned', Mock())
            check.assert_not_called()

    def test_hpa_must_be_owned_active_and_unpaused(self):
        so = scaling.scaled_object()
        so['metadata']['uid'] = 'so'
        hpa = {'metadata': {'name': 'trafficxx-browse', 'ownerReferences': [{'uid': 'so', 'controller': True}]},
            'spec': {'scaleTargetRef': {'name': 'browse'}, 'minReplicas': 2, 'maxReplicas': 4},
            'status': {'conditions': [{'type': 'ScalingActive', 'status': 'True'}]}}
        def kube(*args):
            return json.dumps({'items': [hpa]} if args[1] == 'hpa' else so)
        with patch.object(scaling.base, 'kube', side_effect=kube):
            self.assertEqual(scaling.owned_hpa(), hpa)
            hpa['metadata']['ownerReferences'][0]['uid'] = 'other'
            with self.assertRaisesRegex(ValueError, 'FOREIGN_HPA'):
                scaling.owned_hpa()
            hpa['metadata']['ownerReferences'][0]['uid'] = 'so'
            so['metadata']['annotations'] = {'autoscaling.keda.sh/paused-replicas': '2'}
            with self.assertRaisesRegex(ValueError, 'PAUSED'):
                scaling.owned_hpa()

    def test_database_pressure_stops_on_limit_or_missing_samples(self):
        for field, value in (('cpu_percent', 70), ('memory_percent', 80), ('active_connections', 112)):
            summary = {name: {'observations': 2, 'window_maximum': val} for name, val in
                       (('cpu_percent', 10), ('memory_percent', 20), ('active_connections', 25))}
            summary[field]['window_maximum'] = value
            capacity = Mock(RESOURCE='resource')
            capacity.summarize_metrics.return_value = summary
            with patch.object(scaling, 'load', return_value=capacity), \
                    patch.object(scaling.base, 'command', return_value='{}'), \
                    self.assertRaisesRegex(ValueError, 'PRESSURE_STOP'):
                scaling.database_pressure()

    def test_probe_rejects_unsupported_larger_stages_before_http(self):
        http = Mock()
        with self.assertRaises(ValueError):
            probe.run('a' * 12, users=500, http=http)
        http.assert_not_called()

    def test_native_paced_threads_stop_on_http_error(self):
        # Shortened clock fixture, real ThreadPoolExecutor, no cluster claims.
        http = Mock(return_value=(503, {'detail': {'code': 'DATABASE_BUSY'}}))
        with redirect_stdout(io.StringIO()):
            result = probe.run('a' * 12, http=http)
        self.assertEqual(result['result'], 'BROWSE_SCALING_LOAD_FAILED')
        self.assertIn('UNEXPECTED_HTTP_STATUS_503', result['errors'])
        self.assertLessEqual(result['attempts'], 250)

    def test_manifest_keeps_generator_limits_and_no_sql_identity(self):
        source = (ROOT / 'scripts/browse-autoscaling.py').read_text()
        self.assertNotIn("'scale',", source)
        self.assertIn("'maxSurge': 0", source)
        self.assertIn('stop_owned_job', source)
        self.assertIn('resource_watch.add', source)
        self.assertIn('peak >= 3', source)

    def test_drill_pass_requires_real_scale_up_and_natural_downscale(self):
        for scales, expected_error in (([3, 3, 3, 2], None), ([2, 2, 2], 'NATURAL_SCALE_UP')):
            initial = app()
            pod_names = ['browse-a', 'browse-b', scaling.JOB + '-pod']
            pods = [{'metadata': {'name': name, 'labels': ({'job-name': scaling.JOB} if i == 2 else
                    {'app.kubernetes.io/name': 'trafficxx-browse'})}} for i, name in enumerate(pod_names)]
            outcome = {'result': 'BROWSE_SCALING_LOAD_OK', 'run_id': 'a' * 12,
                'users_with_requests': 250, 'attempts': 21000, 'status_counts': {'200': 21000},
                'errors': {}, 'elapsed_seconds': 180.1}
            states = iter([False, False, True, True])
            def kube(*args, **kwargs):
                if args[:2] == ('get', 'job'):
                    if '--ignore-not-found' in args:
                        return ''
                    terminal = next(states)
                    return json.dumps({'metadata': {'uid': 'job'}, 'status': {'conditions':
                        [{'type': 'Complete', 'status': 'True'}] if terminal else []}})
                if args[:2] == ('get', 'pods'):
                    return json.dumps({'items': pods})
                if args[0] == 'logs':
                    return json.dumps(outcome)
                raise AssertionError(args)
            dates = iter([datetime.now(timezone.utc) - timedelta(seconds=i) for i in (3, 2, 1)])
            def metrics():
                stamp = next(dates).isoformat()
                return {'items': [{'metadata': {'name': name}, 'timestamp': stamp,
                    'containers': [{'name': 'check' if i == 2 else 'browse',
                                    'usage': {'cpu': '1m', 'memory': '40Mi'}}]}
                    for i, name in enumerate(pod_names)]}
            deployments = []
            for replicas in scales:
                row = deepcopy(initial)
                row['spec']['replicas'] = replicas
                row['status']['readyReplicas'] = replicas
                deployments.append(row)
            with ExitStack() as stack:
                for owner, name, kwargs in (
                    (scaling.trial, 'context', {}), (scaling.trial, 'no_active_load', {}),
                    (scaling.trial, 'health', {}), (scaling.trial, 'database_metrics', {'return_value': True}),
                    (scaling, 'database_pressure', {}), (scaling, 'check', {'return_value': initial}),
                    (scaling, 'owned_hpa', {}),
                    (scaling, 'create_job', {'return_value': {'metadata': {'uid': 'job'}}}),
                    (scaling, 'deployment', {'side_effect': deployments}),
                    (scaling.base, 'kube', {'side_effect': kube}),
                    (scaling.trial.users, 'metrics', {'side_effect': metrics}),
                    (scaling.trial.users, 'release_gate', {}),
                    (scaling.time, 'sleep', {}),
                    (scaling, 'uuid4', {'return_value': Mock(hex='a' * 32)})):
                    stack.enter_context(patch.object(owner, name, **kwargs))
                promise = stack.enter_context(patch.object(scaling, 'prometheus'))
                promise.return_value.__enter__.return_value = lambda _: [{'value': [0, '0']}]
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                if expected_error:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        scaling.drill()
                else:
                    scaling.drill()
                    self.assertIn('BROWSE_AUTOSCALING_DRILL_OK', output.getvalue())


if __name__ == '__main__':
    unittest.main()
