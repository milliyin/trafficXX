"""Policy bounds, missing-data handling and real-controller evidence gates."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
import importlib.util
import io
import json
import subprocess
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
    def test_instance_distribution_keeps_missing_distinct_from_zero(self):
        rows = {'errors': [{'metric': {'instance': '10.0.0.1:8000'}, 'value': [240, '1.1']}],
                'borrows': [{'metric': {'instance': '10.0.0.1:8000'}, 'value': [240, '100']},
                            {'metric': {'instance': '10.0.0.2:8000'}, 'value': [240, '0']}]}
        result = scaling.instance_distribution(lambda expression: rows[expression],
                                               {'queue_full': 'errors', 'borrows': 'borrows'})
        self.assertEqual(result['10.0.0.1:8000'], {'queue_full': 1.1, 'borrows': 100})
        self.assertEqual(result['10.0.0.2:8000'], {'queue_full': None, 'borrows': 0})
        self.assertEqual(scaling.instance_distribution(lambda _: [], {'missing': 'query'}), {})
        for invalid in ([rows['errors'][0], rows['errors'][0]],
                        [{'metric': {'instance': 'bad\nlabel'}, 'value': [240, '1']}],
                        [{'metric': {'instance': 'a'}, 'value': [240, 'NaN']}]):
            with self.assertRaises(ValueError):
                scaling.instance_distribution(lambda _: invalid, {'test': 'query'})

    def test_distribution_queries_preserve_tail_and_peak_alignment(self):
        expressions = scaling.distribution_expressions(294, '1000', 354, '1060')
        self.assertEqual(len(expressions), 14)
        for key, expression in expressions.items():
            if key.endswith('_increase'):
                self.assertIn('[354s] @ 1060', expression)
            elif key == 'last_borrow_source_timestamp_after_terminal':
                self.assertIn('@ 1060', expression)
            else:
                self.assertIn('@ 1000', expression)
            self.assertNotIn('sum(', expression)

    def test_distribution_action_is_read_only_and_pinned_to_v2(self):
        job = {'metadata': {'uid': 'owned', 'labels': {'trafficxx.io/run-id': 'a' * 12}},
            'status': {'startTime': '1970-01-01T00:01:00Z', 'conditions': [
                {'type': 'Failed', 'status': 'True', 'lastTransitionTime': '1970-01-01T00:03:00Z'}]}}
        outcome = {'result': 'BROWSE_SCALING_LOAD_FAILED', 'run_id': 'a' * 12,
                   'users': 500, 'start_ramp_seconds': 120}
        def kube(*args):
            self.assertIn(args[0], ('get', 'logs'))
            self.assertIn('browse-users-500-autoscaling-pressure3-v2', ' '.join(args))
            return json.dumps(outcome if args[0] == 'logs' else job)
        def query(expression):
            if expression.startswith('increase(') and 'queue_full' in expression:
                return [{'metric': {'instance': 'a'}, 'value': [240, '1']}]
            return []
        with patch.object(scaling.trial, 'context'), patch.object(scaling.trial, 'no_active_load'), \
                patch.object(scaling.base, 'kube', side_effect=kube), \
                patch.object(scaling, 'prometheus') as connection, \
                patch.object(sys, 'argv', ['browse-autoscaling.py', 'diagnose-500-distribution']), \
                redirect_stdout(io.StringIO()) as output:
            connection.return_value.__enter__.return_value = query
            self.assertEqual(scaling.main(), 0)
        row = json.loads(output.getvalue())
        self.assertEqual(row['per_instance']['a']['queue_full_total_increase'], 1)
        self.assertIsNone(row['per_instance']['a']['borrows_total_increase'])
        self.assertFalse(row['inventory_writes'])

    def test_command_failure_reporting_uses_fixed_labels_and_never_payloads(self):
        secret = 'private-output-never-print-this'
        cases = [
            (['kubectl', '--request-timeout=20s', 'get', '--raw',
              '/apis/metrics.k8s.io/v1beta1/namespaces/trafficxx/pods'], 'read_pod_resource_metrics'),
            (['kubectl', 'get', 'pods', secret], 'read_pods'),
            (['kubectl', 'exec', secret], 'release_load_gate'),
            (['kubectl', 'patch', 'job', secret], 'stop_owned_load_job'),
            (['az', 'monitor', 'metrics', 'list', secret], 'read_azure_database_metrics'),
            ([secret], 'unknown'), (secret, 'unknown')]
        for command, label in cases:
            error = subprocess.CalledProcessError(7, command, output=secret, stderr=secret)
            row = scaling.failure_details(error)
            self.assertEqual(row['command_operation'], label)
            self.assertEqual(row['command_exit_code'], 7)
            self.assertFalse(row['command_timed_out'])
            self.assertNotIn(secret, json.dumps(row))
        self.assertTrue(scaling.failure_details(subprocess.TimeoutExpired(['kubectl', 'get', 'job'], 20))['command_timed_out'])
        self.assertIsNone(scaling.failure_details(ValueError(secret))['check'])
        self.assertEqual(scaling.failure_details(ValueError('JOB_REPLACED'))['check'], 'JOB_REPLACED')

    def test_cleanup_never_touches_terminal_or_replaced_jobs(self):
        with patch.object(scaling.base, 'kube') as kube, patch.object(scaling.trial.users, 'stop_owned_job') as stop:
            self.assertEqual(scaling.cleanup_load({'metadata': {'uid': 'owned'}}, 'job', terminal=True), 'already_terminal')
            kube.assert_not_called()
            stop.assert_not_called()
            kube.return_value = json.dumps({'metadata': {'uid': 'foreign'}})
            with self.assertRaisesRegex(ValueError, 'JOB_REPLACED'):
                scaling.cleanup_load({'metadata': {'uid': 'owned'}}, 'job')
            stop.assert_not_called()
            kube.return_value = json.dumps({'metadata': {'uid': 'owned'}})
            self.assertEqual(scaling.cleanup_load({'metadata': {'uid': 'owned'}}, 'job'), 'stop_requested')
            stop.assert_called_once_with('owned', 'job')

    def test_primary_observer_failure_survives_a_second_cleanup_failure(self):
        primary = subprocess.CalledProcessError(1, ['kubectl', 'get', '--raw',
            '/apis/metrics.k8s.io/v1beta1/namespaces/trafficxx/pods'])
        cleanup = subprocess.CalledProcessError(7, ['kubectl', 'patch', 'job', 'hidden-payload'])
        def kube(*args, **kwargs):
            if '--ignore-not-found' in args:
                return ''
            if args[:2] == ('get', 'job'):
                return json.dumps({'metadata': {'uid': 'job'}, 'status': {}})
            raise AssertionError(args)
        with ExitStack() as stack:
            for owner, name, kwargs in (
                (scaling.trial, 'context', {}), (scaling.trial, 'no_active_load', {}),
                (scaling.trial, 'health', {}), (scaling, 'database_pressure', {}),
                (scaling, 'wait_baseline', {'return_value': app()}),
                (scaling, 'owned_hpa', {'return_value': {'status': {'conditions': [
                    {'type': 'ScalingActive', 'status': 'True'}]}}}),
                (scaling, 'create_job', {'return_value': {'metadata': {'uid': 'job'}}}),
                (scaling, 'deployment', {'return_value': app()}), (scaling, 'prometheus', {}),
                (scaling.base, 'kube', {'side_effect': kube}),
                (scaling.trial.users, 'metrics', {'side_effect': primary}),
                (scaling, 'cleanup_load', {'side_effect': cleanup})):
                stack.enter_context(patch.object(owner, name, **kwargs))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                scaling.drill(users=500, verify_early=True)
            self.assertIs(caught.exception, primary)
            context = primary.observer_context
            self.assertEqual(context['stage'], 'read_resource_metrics')
            self.assertEqual(context['load_cleanup'], 'failed')
            self.assertEqual(context['job'], 'browse-users-500-autoscaling-pressure3-v2')
            self.assertIn('BROWSE_AUTOSCALING_CLEANUP_FAILED', output.getvalue())
            self.assertNotIn('hidden-payload', output.getvalue())

    def test_verification_cli_is_explicit_and_cannot_raise_the_stage(self):
        with patch.object(sys, 'argv', ['browse-autoscaling.py', 'drill', '--users', '500', '--verify-early']), \
                patch.object(scaling, 'drill') as drill:
            self.assertEqual(scaling.main(), 0)
            drill.assert_called_once_with(retry_metric_gap=False, verification_run=False, users=500,
                                          slower_ramp=False, earlier_scaling=False, verify_early=True)
        for argv in (['drill', '--verify-early'], ['check', '--users', '500', '--verify-early'],
                     ['drill', '--users', '500', '--verify-early', '--earlier-scaling']):
            with patch.object(sys, 'argv', ['browse-autoscaling.py', *argv]), \
                    patch.object(scaling.trial, 'context') as context, \
                    patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
                scaling.main()
            context.assert_not_called()

    def test_main_exposes_command_operation_and_stage_without_raw_failure(self):
        error = subprocess.CalledProcessError(9, ['az', 'monitor', 'metrics', 'list', 'private-argv'],
                                             output='private-stdout', stderr='private-stderr')
        error.observer_context = {'stage': 'observe_database_metrics', 'load_cleanup': 'stop_requested'}
        with patch.object(sys, 'argv', ['browse-autoscaling.py', 'drill', '--users', '500', '--verify-early']), \
                patch.object(scaling, 'drill', side_effect=error), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(scaling.main(), 1)
        row = json.loads(output.getvalue())
        self.assertEqual(row['command_operation'], 'read_azure_database_metrics')
        self.assertEqual(row['command_exit_code'], 9)
        self.assertEqual(row['observer']['stage'], 'observe_database_metrics')
        self.assertNotIn('private-', output.getvalue())

    def test_early_configuration_changes_one_field_and_resumes_without_repatch(self):
        for old in ('5', '3'):
            so = scaling.scaled_object(old)
            so['metadata'].update(uid='so', resourceVersion='17')
            hpa = {'metadata': {'ownerReferences': [{'uid': 'so', 'controller': True}]}}
            writes = []
            def kube(*args):
                if args[0] == 'patch':
                    operations = json.loads(args[-1])
                    self.assertEqual(operations[:3], [
                        {'op': 'test', 'path': '/metadata/uid', 'value': 'so'},
                        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '17'},
                        {'op': 'test', 'path': '/spec', 'value': so['spec']}])
                    self.assertEqual(operations[3:], [{'op': 'replace',
                        'path': '/spec/triggers/0/metadata/threshold', 'value': '3'}])
                    writes.append(args)
                    return ''
                self.assertEqual(args[:3], ('get', 'scaledobject', 'browse'))
                return json.dumps(so)
            with ExitStack() as stack:
                for owner, name, kwargs in (
                    (scaling.trial, 'context', {}), (scaling.trial, 'no_active_load', {}),
                    (scaling.trial, 'health', {}), (scaling, 'database_pressure', {}),
                    (scaling, 'wait_baseline', {'return_value': app()}),
                    (scaling, 'owned_hpa', {'return_value': hpa}),
                    (scaling, 'deployment', {'return_value': app()}),
                    (scaling, 'check', {}), (scaling, 'prometheus', {}),
                    (scaling.base, 'kube', {'side_effect': kube}),
                    (scaling, 'wait_until', {'side_effect': lambda fn, _: fn()})):
                    stack.enter_context(patch.object(owner, name, **kwargs))
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                scaling.configure_early()
            self.assertEqual(len(writes), int(old == '5'))
            self.assertIn('BROWSE_EARLY_SCALING_CONFIGURED', output.getvalue())

    def test_early_target_reconciliation_refuses_old_or_missing_native_target(self):
        hpa = {'spec': {'metrics': [{'type': 'External', 'external': {
            'metric': {'name': 's' + str(i) + '-prometheus'},
            'target': {'type': 'AverageValue', 'averageValue': v}}} for i, v in enumerate(('3', '40'))]}}
        scaling.require_hpa_targets(hpa, '3')
        hpa['spec']['metrics'][0]['external']['target']['averageValue'] = '5'
        with self.assertRaisesRegex(ValueError, 'NOT_RECONCILED'):
            scaling.require_hpa_targets(hpa, '3')
        with self.assertRaisesRegex(ValueError, 'NOT_RECONCILED'):
            scaling.require_hpa_targets({'spec': {}}, '3')
        with self.assertRaisesRegex(ValueError, 'UNREVIEWED'):
            scaling.scaled_object('2')

    def test_early_cli_requires_explicit_500_stage(self):
        with patch.object(sys, 'argv', ['browse-autoscaling.py', 'drill', '--users', '500', '--earlier-scaling']), \
                patch.object(scaling, 'drill') as drill:
            self.assertEqual(scaling.main(), 0)
            drill.assert_called_once_with(retry_metric_gap=False, verification_run=False, users=500,
                                          slower_ramp=False, earlier_scaling=True, verify_early=False)
        for argv in (['drill', '--earlier-scaling'], ['check', '--earlier-scaling'],
                     ['drill', '--users', '500', '--slower-ramp', '--earlier-scaling']):
            with patch.object(sys, 'argv', ['browse-autoscaling.py', *argv]), \
                    patch.object(scaling.trial, 'context') as context, \
                    patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
                scaling.main()
            context.assert_not_called()

    def test_historical_diagnostics_are_read_only_and_job_aligned(self):
        job = {'metadata': {'uid': 'owned', 'labels': {'trafficxx.io/run-id': 'a' * 12}},
            'status': {'startTime': '1970-01-01T00:01:00Z', 'conditions': [
                {'type': 'Failed', 'status': 'True', 'lastTransitionTime': '1970-01-01T00:03:00Z'}]}}
        outcome = {'result': 'BROWSE_SCALING_LOAD_FAILED', 'run_id': 'a' * 12, 'users': 500}
        expressions = []
        def query(expression):
            expressions.append(expression)
            return [{'value': [180, '1.25']}] if 'queue_full' in expression else []
        def kube(*args):
            self.assertIn(args[0], ('get', 'logs'))
            return json.dumps(outcome if args[0] == 'logs' else job)
        with patch.object(scaling.trial, 'context'), patch.object(scaling.trial, 'no_active_load'), \
                patch.object(scaling.base, 'kube', side_effect=kube), \
                patch.object(scaling, 'prometheus') as connection, redirect_stdout(io.StringIO()) as output:
            connection.return_value.__enter__.return_value = query
            scaling.diagnose_500()
        row = json.loads(output.getvalue())
        self.assertEqual(row['query_window_seconds'], 180)
        self.assertEqual(row['counter_query_window_seconds'], 240)
        self.assertEqual(row['post_terminal_observation_seconds'], 60)
        self.assertEqual(row['counter_increases']['queue_full_total'], 1.25)
        self.assertIsNone(row['counter_increases']['acquire_timeouts_total'])
        self.assertEqual(len(expressions), 13)
        self.assertTrue(all('@ 240' in expression for expression in expressions[:7]))
        self.assertTrue(all('@ 180' in expression for expression in expressions[7:11]))
        self.assertIsNone(row['post_job_sample_coverage']['all_original_instances_sampled_after_terminal'])

    def test_post_job_coverage_requires_every_original_instance_and_new_samples(self):
        before = [{'metric': {'instance': name}} for name in ('a', 'b')]
        def after(a, b):
            return [{'metric': {'instance': name}, 'value': [240, stamp]} for name, stamp in (('a', a), ('b', b))]
        key = 'all_original_instances_sampled_after_terminal'
        self.assertTrue(scaling.post_job_coverage(before, after(195, 210), 180)[key])
        self.assertFalse(scaling.post_job_coverage(before, after(180, 210), 180)[key])
        self.assertFalse(scaling.post_job_coverage(before, after(195, 210)[:1], 180)[key])
        with self.assertRaises(ValueError):
            scaling.post_job_coverage(before, after(float('nan'), 210), 180)

    def test_historical_diagnostic_refuses_active_or_unbounded_jobs(self):
        with self.assertRaisesRegex(ValueError, 'NOT_TERMINAL'):
            scaling.diagnostic_window({'status': {}})
        job = {'status': {'startTime': '2026-10-27T10:00:00Z', 'conditions': [
            {'type': 'Failed', 'status': 'True', 'lastTransitionTime': '2026-10-27T11:00:00Z'}]}}
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_DIAGNOSTIC_WINDOW'):
            scaling.diagnostic_window(job)
        self.assertIsNone(scaling.optional_scalar(lambda _: [], 'query'))
        with self.assertRaises(ValueError):
            scaling.optional_scalar(lambda _: [{'value': [0, 'NaN']}], 'query')

    def test_preflight_waits_for_discovery_but_requires_the_same_strict_check(self):
        ready = app()
        checking = Mock(side_effect=[ValueError('DISCOVERY_NOT_CONVERGED'),
                                     ValueError('HPA_METRIC_NOT_ACTIVE'), ready])
        sleeping = Mock()
        with patch.object(scaling, 'check', checking), redirect_stdout(io.StringIO()):
            self.assertEqual(scaling.wait_baseline(Mock(), clock=lambda: 0, sleep=sleeping), ready)
        self.assertEqual(checking.call_count, 3)
        self.assertEqual(sleeping.call_count, 2)

    def test_preflight_convergence_is_bounded_and_policy_errors_are_immediate(self):
        with patch.object(scaling, 'check', side_effect=ValueError('DISCOVERY_NOT_CONVERGED')), \
                redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'CONVERGENCE_TIMEOUT'):
            scaling.wait_baseline(Mock(), clock=Mock(side_effect=[0, 180]), sleep=Mock())
        for reason in ('FOREIGN_HPA', 'POOL_POLICY_CHANGED', 'REPLICA_BUDGET_EXCEEDED', 'AUTOSCALING_PAUSED'):
            sleeping = Mock()
            with patch.object(scaling, 'check', side_effect=ValueError(reason)), self.assertRaisesRegex(ValueError, reason):
                scaling.wait_baseline(Mock(), clock=lambda: 0, sleep=sleeping)
            sleeping.assert_not_called()

    def test_metric_gap_requires_recovery_within_a_bounded_total(self):
        active = {'status': {'conditions': [{'type': 'ScalingActive', 'status': 'True'}]}}
        inactive = {'status': {'conditions': [{'type': 'ScalingActive', 'status': 'False'}]}}
        with redirect_stdout(io.StringIO()):
            watch = scaling.MetricReadiness()
            watch.observe(inactive, 10)
            watch.observe(inactive, 25)
            with self.assertRaisesRegex(ValueError, 'UNRESOLVED'):
                watch.summary()
            watch.observe(active, 40)
            self.assertEqual(watch.summary(), {'gap_count': 1, 'total_gap_seconds': 30,
                                               'maximum_gap_seconds': 30})
            watch.observe(inactive, 50)
            with self.assertRaisesRegex(ValueError, 'TIMEOUT'):
                watch.observe(inactive, 80)
            watch = scaling.MetricReadiness()
            watch.observe(inactive, 0)
            with self.assertRaisesRegex(ValueError, 'TIMEOUT'):
                watch.observe(active, 60)

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

    def test_recovery_only_waits_for_natural_idle_and_never_creates_load(self):
        initial = app()
        initial['spec']['replicas'] = 3
        with ExitStack() as stack:
            for owner, name in ((scaling.trial, 'context'), (scaling.trial, 'no_active_load'),
                                (scaling.trial, 'health'), (scaling, 'database_pressure')):
                stack.enter_context(patch.object(owner, name))
            stack.enter_context(patch.object(scaling, 'wait_baseline', return_value=initial))
            stack.enter_context(patch.object(scaling, 'prometheus'))
            idle = stack.enter_context(patch.object(scaling, 'idle_two', return_value=app()))
            waiting = stack.enter_context(patch.object(scaling, 'wait_until', side_effect=lambda fn, _: fn()))
            creating = stack.enter_context(patch.object(scaling, 'create_job'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            scaling.wait_idle()
            creating.assert_not_called()
            idle.assert_called_once()
            self.assertEqual(waiting.call_args.args[1], 600)

    def test_retry_waits_for_idle_before_creating_a_distinct_job(self):
        initial = app()
        initial['spec']['replicas'] = 4
        with ExitStack() as stack:
            for owner, name in ((scaling.trial, 'context'), (scaling.trial, 'no_active_load'),
                                (scaling.trial, 'health'), (scaling, 'database_pressure')):
                stack.enter_context(patch.object(owner, name))
            stack.enter_context(patch.object(scaling.base, 'kube', return_value=''))
            stack.enter_context(patch.object(scaling, 'check', return_value=initial))
            stack.enter_context(patch.object(scaling, 'owned_hpa'))
            stack.enter_context(patch.object(scaling, 'uuid4', return_value=Mock(hex='a' * 32)))
            promise = stack.enter_context(patch.object(scaling, 'prometheus'))
            promise.return_value.__enter__.return_value = Mock()
            waiting = stack.enter_context(patch.object(scaling, 'wait_until', return_value=app()))
            def create(initial, run_id, name):
                waiting.assert_called_once()
                self.assertEqual(initial['spec']['replicas'], 2)
                self.assertEqual(name, scaling.JOB + '-v2')
                raise ValueError('STOP_BEFORE_JOB_CREATION')
            creating = stack.enter_context(patch.object(scaling, 'create_job', side_effect=create))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(ValueError, 'START_WITH_TWO'):
                scaling.drill()
            creating.assert_not_called()
            waiting.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'STOP_BEFORE_JOB_CREATION'):
                scaling.drill(retry_metric_gap=True)

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
            so['spec'] = scaling.scaled_object('3')['spec']
            self.assertEqual(scaling.owned_hpa(), hpa)
            with self.assertRaisesRegex(ValueError, 'POLICY_CHANGED'):
                scaling.owned_hpa(pressure_target='5')
            so['spec']['triggers'][0]['metadata']['threshold'] = '2'
            with self.assertRaisesRegex(ValueError, 'POLICY_CHANGED'):
                scaling.owned_hpa()
            so['spec'] = scaling.scaled_object()['spec']
            hpa['status']['conditions'][0]['status'] = 'False'
            with self.assertRaisesRegex(ValueError, 'METRIC_NOT_ACTIVE'):
                scaling.owned_hpa()
            self.assertEqual(scaling.owned_hpa(require_active=False), hpa)
            hpa['metadata']['ownerReferences'][0]['uid'] = 'other'
            with self.assertRaisesRegex(ValueError, 'FOREIGN_HPA'):
                scaling.owned_hpa(require_active=False)
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
            probe.run('a' * 12, users=1000, http=http)
        http.assert_not_called()

    def test_500_stage_requires_matching_pacing_counts_and_no_errors(self):
        row = {'result': 'BROWSE_SCALING_LOAD_OK', 'run_id': 'a' * 12, 'users': 500,
            'users_with_requests': 500, 'scheduled_duration_seconds': 180, 'elapsed_seconds': 182,
            'start_ramp_seconds': 60, 'think_seconds': 2, 'max_workload_attempts': 45000,
            'attempts': 35000, 'status_counts': {'200': 35000}, 'errors': {}}
        scaling.validate_load(row, 'a' * 12, 500, True)
        for key, value in (('users', 250), ('users_with_requests', 499), ('start_ramp_seconds', 2),
                ('max_workload_attempts', 22500), ('errors', {'503': 1}), ('attempts', 45001),
                ('elapsed_seconds', 60)):
            changed = dict(row, **{key: value})
            with self.assertRaises(ValueError):
                scaling.validate_load(changed, 'a' * 12, 500, True)
        with self.assertRaises(ValueError):
            scaling.validate_load(row, 'a' * 12, 500, False)

    def test_500_cli_propagates_only_the_selected_supported_stage(self):
        with patch.object(sys, 'argv', ['browse-autoscaling.py', 'drill', '--users', '500']), \
                patch.object(scaling, 'drill') as drill:
            self.assertEqual(scaling.main(), 0)
            drill.assert_called_once_with(retry_metric_gap=False, verification_run=False, users=500, slower_ramp=False,
                                          earlier_scaling=False, verify_early=False)
        with patch.object(sys, 'argv', ['browse-autoscaling.py', 'drill', '--users', '500', '--slower-ramp']), \
                patch.object(scaling, 'drill') as drill:
            self.assertEqual(scaling.main(), 0)
            drill.assert_called_once_with(retry_metric_gap=False, verification_run=False, users=500, slower_ramp=True,
                                          earlier_scaling=False, verify_early=False)
        for argv in (['drill', '--users', '1000'], ['check', '--users', '500'],
                     ['drill', '--users', '500', '--verification-run'], ['drill', '--slower-ramp']):
            with patch.object(sys, 'argv', ['browse-autoscaling.py', *argv]), \
                    patch.object(scaling.trial, 'context') as context, \
                    patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
                scaling.main()
            context.assert_not_called()

    def test_500_generator_ramp_and_attempt_cap_with_simulated_time(self):
        waits = []
        class Event:
            def wait(self, seconds):
                waits.append(seconds)
                return False
            def is_set(self):
                return False
            def set(self):
                raise AssertionError('Unexpected workload failure')
        seated = {'id': 'demo-seated', 'inventory_type': 'SEATED', 'available': 100,
                  'seats': ['A' + str(n) for n in range(1, 101)]}
        general = {'id': 'demo-general', 'inventory_type': 'GENERAL', 'available': 100, 'seats': None}
        def http(url, customer):
            return 200, seated if 'demo-seated' in url else general
        # Execute every user and request with fake waits and time. This tests
        # scheduling/caps only; it is not real load or a latency measurement.
        for ramp in (60, 120):
            waits.clear()
            with patch.object(probe, 'ThreadPoolExecutor') as executor, \
                    patch.object(probe.threading, 'Event', Event), \
                    patch.object(probe.time, 'monotonic', return_value=0), redirect_stdout(io.StringIO()):
                executor.return_value.__enter__.return_value.map.side_effect = lambda fn, ids: [fn(i) for i in ids]
                result = probe.run('a' * 12, users=500, ramp_seconds=ramp, http=http)
            self.assertEqual(result['attempts'], 45000)
            self.assertEqual(result['users_with_requests'], 500)
            self.assertEqual(result['status_counts'], {'200': 45000})
            self.assertEqual(result['start_ramp_seconds'], ramp)
            self.assertAlmostEqual(waits[499 * 91], 499 * ramp / 500)
            executor.assert_called_once_with(max_workers=500)

    def test_slow_ramp_must_be_verified_and_cannot_change_arbitrary_pacing(self):
        row = {'result': 'BROWSE_SCALING_LOAD_OK', 'run_id': 'a' * 12, 'users': 500,
            'users_with_requests': 500, 'scheduled_duration_seconds': 180, 'elapsed_seconds': 182,
            'start_ramp_seconds': 120, 'think_seconds': 2, 'max_workload_attempts': 45000,
            'attempts': 28000, 'status_counts': {'200': 28000}, 'errors': {}}
        scaling.validate_load(row, 'a' * 12, 500, True, slower_ramp=True)
        with self.assertRaisesRegex(ValueError, 'PACING_NOT_VERIFIED'):
            scaling.validate_load(row, 'a' * 12, 500, True)
        with self.assertRaises(ValueError):
            probe.run('a' * 12, users=500, ramp_seconds=90, http=Mock())

    def test_500_job_receives_exact_user_count_without_raising_resource_limits(self):
        initial = app()
        initial['spec']['template']['spec']['containers'][0]['image'] = 'immutable-image'
        patches = []
        def kube(*args, data=None):
            if args[:2] == ('create', 'configmap'):
                return '{"kind":"ConfigMap"}'
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [{'metadata': {}, 'spec': {'nodeName': 'existing-node'}}]})
            if args[:2] == ('patch', '--local'):
                patches.extend(json.loads(args[args.index('--patch') + 1]))
                return '{"kind":"Job"}'
            if args[:2] == ('create', '-f'):
                return '{"metadata":{"uid":"job"}}'
            return ''
        with patch.object(scaling.base, 'kube', side_effect=kube):
            scaling.create_job(initial, 'a' * 12, 'browse-users-500-autoscaling', users=500)
        args = next(p['value'] for p in patches if p['path'].endswith('/args'))
        self.assertEqual(args, ['a' * 12, '500'])
        self.assertFalse(any('/resources' in p['path'] for p in patches))
        self.assertEqual(next(p['value'] for p in patches if p['path'] == '/metadata/name'),
                         'browse-users-500-autoscaling')
        patches.clear()
        with patch.object(scaling.base, 'kube', side_effect=kube):
            scaling.create_job(initial, 'a' * 12, 'browse-users-500-autoscaling-ramp120', users=500, slower_ramp=True)
        self.assertEqual(next(p['value'] for p in patches if p['path'].endswith('/args')), ['a' * 12, '500', '120'])
        self.assertFalse(any('/resources' in p['path'] for p in patches))

    def test_native_paced_threads_stop_on_http_error(self):
        # Shortened clock fixture, real ThreadPoolExecutor, no cluster claims.
        http = Mock(return_value=(503, {'detail': {'code': 'DATABASE_BUSY'}}))
        with redirect_stdout(io.StringIO()):
            result = probe.run('a' * 12, http=http)
        self.assertEqual(result['result'], 'BROWSE_SCALING_LOAD_FAILED')
        self.assertIn('UNEXPECTED_HTTP_STATUS_503', result['errors'])
        self.assertLessEqual(result['attempts'], 250)

    def test_first_failure_records_only_allowlisted_code_not_raw_payload(self):
        for code, expected in (('DATABASE_BUSY', 'DATABASE_BUSY'), ('private-value', None)):
            http = Mock(return_value=(503, {'detail': {'code': code, 'extra': 'private-payload'}}))
            with redirect_stdout(io.StringIO()):
                row = probe.run('a' * 12, users=500, http=http)
            self.assertEqual(row['first_failure']['service_error_code'], expected)
            self.assertEqual(row['first_failure']['http_status'], 503)
            self.assertNotIn('private-value', json.dumps(row))
            self.assertNotIn('private-payload', json.dumps(row))

    def test_manifest_keeps_generator_limits_and_no_sql_identity(self):
        source = (ROOT / 'scripts/browse-autoscaling.py').read_text()
        self.assertNotIn("'scale',", source)
        self.assertIn("'maxSurge': 0", source)
        self.assertIn('stop_owned_job', source)
        self.assertIn('resource_watch.add', source)
        self.assertIn('peak >= 3', source)

    def test_drill_pass_requires_real_scale_up_and_natural_downscale(self):
        for scales, expected_error, retry, verification, users, slower, early, verify in (([3, 3, 3, 2], None, False, False, 250, False, False, False),
                ([2, 2, 2], 'NATURAL_SCALE_UP', False, False, 250, False, False, False), ([3, 3, 3, 2], None, True, False, 250, False, False, False),
                ([3, 3, 3, 2], None, False, True, 250, False, False, False), ([4, 4, 4, 2], None, False, False, 500, False, False, False),
                ([4, 4, 4, 2], None, False, False, 500, True, False, False),
                ([3, 3, 3, 2], None, False, False, 500, False, True, False),
                ([4, 4, 4, 2], None, False, False, 500, False, False, True)):
            initial = app()
            job_name = ('browse-users-500-autoscaling-pressure3-v2' if verify else
                        'browse-users-500-autoscaling-pressure3' if early else
                        ('browse-users-500-autoscaling-ramp120' if slower else 'browse-users-500-autoscaling') if users == 500 else
                        scaling.JOB + ('-v3' if verification else '-v2' if retry else ''))
            pod_names = ['browse-a', 'browse-b', job_name + '-pod']
            pods = [{'metadata': {'name': name, 'labels': ({'job-name': scaling.JOB} if i == 2 else
                    {'app.kubernetes.io/name': 'trafficxx-browse'})}} for i, name in enumerate(pod_names)]
            pods[2]['metadata']['labels']['job-name'] = job_name
            outcome = {'result': 'BROWSE_SCALING_LOAD_OK', 'run_id': 'a' * 12,
                'users_with_requests': users, 'attempts': 21000, 'status_counts': {'200': 21000},
                'errors': {}, 'elapsed_seconds': 180.1}
            if users == 500:
                outcome.update(users=500, scheduled_duration_seconds=180, start_ramp_seconds=120 if slower or early or verify else 60,
                               think_seconds=2, max_workload_attempts=45000)
            states = iter([False, False, True])
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
                    (scaling, 'owned_hpa', {'return_value': {'status': {'conditions': [
                        {'type': 'ScalingActive', 'status': 'True'}]}}}),
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
                        scaling.drill(retry_metric_gap=retry, verification_run=verification, users=users,
                                      slower_ramp=slower, earlier_scaling=early, verify_early=verify)
                else:
                    scaling.drill(retry_metric_gap=retry, verification_run=verification, users=users,
                                  slower_ramp=slower, earlier_scaling=early, verify_early=verify)
                    self.assertIn('BROWSE_AUTOSCALING_DRILL_OK', output.getvalue())
                    if early or verify:
                        self.assertIn(job_name, output.getvalue())
                        self.assertIn('"pressure_target_per_pod": 3', output.getvalue())
                    if retry or verification:
                        self.assertIn(job_name, output.getvalue())
                    # The third poll is already Job Complete. Live generator
                    # metrics must not be read after that terminal evidence.
                    with self.assertRaises(StopIteration):
                        next(states)
                    self.assertIsNotNone(next(dates))

    def test_conflicting_run_modes_fail_before_cloud_access(self):
        with patch.object(scaling.trial, 'context') as context:
            with self.assertRaisesRegex(ValueError, 'CONFLICTING_DRILL_MODES'):
                scaling.drill(retry_metric_gap=True, verification_run=True)
            with self.assertRaisesRegex(ValueError, 'CONFLICTING_DRILL_MODES'):
                scaling.drill(users=500, verification_run=True)
            with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_USER_COUNT'):
                scaling.drill(users=1000)
            with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_RAMP'):
                scaling.drill(slower_ramp=True)
            context.assert_not_called()


if __name__ == '__main__':
    unittest.main()
