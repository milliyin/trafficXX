"""Read-only user pacing, resource budgets and diagnostic Job ownership gates."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('users_fixtures', 'unit_tests/test_read_load.py')
with patch.dict(sys.modules, {'read_probe': fixtures.probe}):
    probe = load('browse_users_probe', 'scripts/check-browse-users.py')
runner = load('browse_users_runner', 'scripts/run-browse-users.py')


class BrowseUsers(unittest.TestCase):
    def test_resource_gate_blocks_http_until_same_run_is_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'gate'
            def ready(seconds):
                path.write_text(fixtures.config()['run_id'])
            with redirect_stdout(io.StringIO()):
                probe.wait_for_resource_gate(fixtures.config(), path, sleep=ready, clock=lambda: 0)
                path.write_text('another-run')
                with self.assertRaises(fixtures.probe.CheckFailed):
                    probe.wait_for_resource_gate(fixtures.config(), path)
        with patch.object(probe, 'wait_for_resource_gate', side_effect=fixtures.probe.CheckFailed('NOT_READY')), \
                patch.object(probe, 'run') as workload, self.assertRaises(fixtures.probe.CheckFailed):
            probe.execute(fixtures.config())
        workload.assert_not_called()

    def test_fifty_paced_users_read_only_the_service_and_keep_inventory(self):
        config, http, urls = fixtures.config(), fixtures.Http(), []

        def service(url, customer):
            self.assertTrue(url.startswith('http://browse.trafficxx.svc.cluster.local/events/demo-'))
            urls.append(url)
            return http(config['targets']['browse'][len(urls) % 2], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=.3, think=.02)
        self.assertEqual(result['users_with_requests'], 50)
        self.assertTrue(50 <= result['attempts'] <= 1500)
        self.assertEqual(result['attempts'], result['valid_responses'])
        self.assertLessEqual(result['sampled_peak_workload_http_in_flight'], 50)
        self.assertFalse(result['inventory_writes'])
        self.assertFalse(result['latency_slo_tested'])
        self.assertEqual(len(result['pools']), 2)
        self.assertTrue(all(api == 'browse' for api, _, _ in http.calls))

    def test_unexpected_http_response_stops_without_printing_response_body(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(fixtures.config(), fixtures.Http(), lambda *args: (503, {'PRIVATE': 'BODY'}),
                      duration=.3, think=.02)
        self.assertIn('BROWSE_USERS_PHASE_FAILED', output.getvalue())
        self.assertIn('503', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_pool_errors_or_initial_inventory_changes_prevent_success(self):
        for fault in ('observer', 'inventory'):
            with self.subTest(fault=fault), redirect_stdout(io.StringIO()), \
                    self.assertRaises(fixtures.probe.CheckFailed):
                probe.run(fixtures.config(), fixtures.Http(fault), lambda *args: (200, {}),
                          duration=.3, think=.02)

    def test_sustained_sampled_pool_waiting_stops_new_reads(self):
        clock = [0]
        class Waiting(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool' and self.snapshots[target['pod']] > 1:
                    clock[0] += 11
                    body['statistics']['requests_waiting'] = 1
                return status, body
        http = Waiting()
        config = fixtures.config()
        def service(url, customer):
            return http(config['targets']['browse'][0], '/' + url.split('/', 3)[3], customer)
        with patch.object(probe.time, 'monotonic', side_effect=lambda: clock[0]), \
                redirect_stdout(io.StringIO()) as output, self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(config, http, service, duration=40, think=.02)
        self.assertIn('SAMPLED_POOL_WAITING_TEN_SECONDS', output.getvalue())

    def test_resource_units_freshness_and_container_identity(self):
        self.assertAlmostEqual(runner.quantity('120000000n', 'cpu'), .12)
        self.assertEqual(runner.quantity('500m', 'cpu'), .5)
        self.assertEqual(runner.quantity('65536Ki', 'memory'), 64)
        for value in ('NaN', '-1', '1SECRET'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.quantity(value, 'cpu')
        payload = {'items': [{'metadata': {'name': 'browse-a'},
                             'timestamp': '2026-10-24T10:00:00.123456789Z',
                             'containers': [{'name': 'browse', 'usage': {'cpu': '100m', 'memory': '64Mi'}}]}]}
        expected = {'browse-a': {'container': 'browse', 'cpu': .5, 'memory': 256}}
        now = datetime(2026, 10, 24, 10, 0, 2, tzinfo=timezone.utc).timestamp()
        self.assertEqual(runner.sample(payload, expected, now)['browse-a']['cpu_limit_fraction'], .2)
        with self.assertRaises(runner.MetricSampleError) as caught:
            runner.sample(payload, expected, now + 100)
        self.assertEqual(caught.exception.details['pod'], 'browse-a')
        self.assertGreater(caught.exception.details['age_seconds'], 60)
        payload['items'][0]['containers'][0]['name'] = 'foreign'
        with self.assertRaises(ValueError):
            runner.sample(payload, expected, now)

    def test_sixty_second_collector_has_bounded_delay_allowance(self):
        payload = {'items': [{'metadata': {'name': 'browse-a'},
                             'timestamp': '2026-10-24T12:44:26Z',
                             'containers': [{'name': 'browse', 'usage': {'cpu': '10m', 'memory': '59Mi'}}]}]}
        expected = {'browse-a': {'container': 'browse', 'cpu': .5, 'memory': 256}}
        stamp = datetime(2026, 10, 24, 12, 44, 26, tzinfo=timezone.utc).timestamp()
        for age in (62.375, 90):
            self.assertIn('browse-a', runner.sample(payload, expected, stamp + age))
        for age in (90.001, -5.001):
            with self.assertRaises(runner.MetricSampleError) as caught:
                runner.sample(payload, expected, stamp + age)
            self.assertEqual(caught.exception.details['max_age_seconds'], 90)

    def test_resource_pressure_must_persist_and_duplicate_samples_are_not_new_evidence(self):
        watch = runner.ResourceWatch()
        high = {'pod': {'timestamp': 'same', 'cpu_cores': .45, 'memory_mib': 64,
                        'cpu_limit_fraction': .9, 'memory_limit_fraction': .25}}
        watch.add(high, 0)
        watch.add(high, 19)
        self.assertEqual(watch.summary()['pod']['distinct_samples'], 1)
        with self.assertRaisesRegex(ValueError, 'RESOURCE_STOP_CPU'):
            watch.add(high, 20)
        watch = runner.ResourceWatch()
        watch.add(high, 0)
        low = deepcopy(high)
        low['pod']['cpu_limit_fraction'] = .1
        watch.add(low, 10)
        watch.add(high, 30)  # Low sample reset the high-CPU hold.
        self.assertEqual(watch.summary()['pod']['sampled_max_cpu_cores'], .45)

    def test_memory_budget_and_job_stop_are_scoped(self):
        watch = runner.ResourceWatch()
        values = {'generator': {'timestamp': 'one', 'cpu_cores': .01, 'memory_mib': 230,
                                'cpu_limit_fraction': .01, 'memory_limit_fraction': .9}}
        watch.add(values, 0)
        with self.assertRaisesRegex(ValueError, 'RESOURCE_STOP_MEMORY'):
            watch.add(values, 10)
        with patch.object(runner.base, 'kube') as kube:
            runner.stop_owned_job('owned-job-uid')
        args = kube.call_args.args
        self.assertEqual(args[:3], ('patch', 'job', runner.JOB))
        operations = json.loads(args[-1])
        self.assertEqual(operations[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'owned-job-uid'})
        self.assertEqual(operations[1]['value'], 1)


class BrowseUsersRunner(unittest.TestCase):
    def scenario(self, fault=None, preflight_only=False):
        commands, polls = [], [0]
        generator = runner.JOB + '-pod'
        now = datetime.now(timezone.utc).isoformat()
        config = fixtures.config()
        counts = {}

        def snapshot(flow, api):
            counts[api] = counts.get(api, 0) + 1
            return {'generation': 2 if fault == 'rollout' and counts[api] > 1 else 1,
                    'pods': [dict(row, image='trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'a' * 64)
                             for row in config['targets'][api]]}

        def resources():
            return {'items': [{'metadata': {'name': name},
                               'timestamp': '2020-01-01T00:00:00Z'
                               if fault == 'stale-generator' and polls[0] == 1 and name == generator else now,
                               'containers': [{'name': 'check' if name == generator else 'browse',
                                               'usage': {'cpu': '10m', 'memory': '32Mi'}}]}
                              for name in ('browse-0', 'browse-1', generator)]}

        def kube(*args, **kwargs):
            commands.append(args)
            if args == ('config', 'current-context'):
                return 'trafficxx-lab-aks'
            if args[:2] == ('auth', 'can-i'):
                return 'yes'
            if args[:2] == ('get', 'job') and '--ignore-not-found' in args:
                return 'job/existing' if fault == 'existing' else ''
            if args[:2] == ('get', 'jobs'):
                return '{"items":[]}'
            if args[:2] == ('get', 'hpa'):
                return json.dumps({'items': [{'spec': {'scaleTargetRef': {'kind': 'Deployment', 'name': 'browse'}}}]
                                  if fault == 'hpa' else []})
            if args[:2] == ('get', 'pod'):
                return '{"spec":{"nodeName":"existing-apps"}}'
            if args[:2] == ('get', 'node'):
                return json.dumps({'metadata': {'labels': {'agentpool': 'system' if fault == 'bad-node' else 'apps'}}, 'spec': {},
                                   'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}})
            if args[:2] == ('get', 'deployment'):
                return json.dumps({'spec': {'template': {'spec': {'containers': [
                    {'name': 'browse', 'resources': {'limits': {'cpu': '500m', 'memory': '256Mi'}}}]}}}})
            if args[:2] == ('create', 'configmap') or args[:2] == ('patch', '--local'):
                return '{}'
            if args[:2] == ('create', '-f') and '-o' in args:
                if fault in ('lost-create', 'foreign-create'):
                    raise subprocess.TimeoutExpired('kubectl', 30)
                return '{"metadata":{"uid":"owned"}}'
            if args[:2] == ('get', 'job'):
                polls[0] += 1
                return json.dumps({'metadata': {'uid': 'owned'},
                    'spec': {'template': {'spec': {'volumes': [{'configMap': {'name':
                        'foreign' if fault == 'foreign-create' else 'browse-users-abcdef123456-files'}}]}}},
                    'status': {'succeeded': 1} if polls[0] > 2 else {}})
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [{'metadata': {'name': generator, 'ownerReferences': [
                    {'controller': True, 'kind': 'Job', 'uid': 'owned'}]},
                    'status': {'phase': 'Succeeded' if polls[0] > 2 else 'Running',
                               'containerStatuses': [{'restartCount': 0}]}}]})
            if args[0] == 'logs':
                return json.dumps({'result': 'BROWSE_USERS_WORKLOAD_OK', 'run_id': 'abcdef123456'})
            return '{}'

        def add(watch, values, clock):
            if fault == 'resource-stop':
                raise ValueError('RESOURCE_STOP_CPU_LIMIT_FRACTION')
            for name in values:
                watch.rows[name] = {'timestamps': {'a', 'b'}, 'max_cpu_cores': .01, 'max_memory_mib': 32}
        output = io.StringIO()
        with patch.object(runner.base, 'command', return_value='35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'), \
                patch.object(runner.base, 'kube', side_effect=kube), \
                patch.object(runner.base, 'snapshot', side_effect=snapshot), \
                patch.object(runner, 'metrics', side_effect=RuntimeError('PRIVATE') if fault == 'no-metrics' else resources), \
                patch.object(runner.ResourceWatch, 'add', add), \
                patch.object(runner, 'validate', side_effect=ValueError('INCOMPLETE') if fault == 'invalid' else None), \
                patch.object(runner, 'uuid4', return_value=type('Id', (), {'hex': 'abcdef123456'})()), \
                patch.object(runner.time, 'sleep'), redirect_stdout(output):
            result = runner.execute(preflight_only=preflight_only)
        return result, output.getvalue(), commands

    def test_success_requires_resource_and_job_evidence_and_unchanged_pods(self):
        code, output, commands = self.scenario()
        self.assertEqual(code, 0)
        self.assertIn('BROWSE_USERS_50_OK', output)
        self.assertFalse(any(args[0] in ('delete', 'scale') for args in commands))

    def test_stale_startup_generator_is_excluded_until_fresh_then_gate_is_released(self):
        code, output, commands = self.scenario('stale-generator')
        self.assertEqual(code, 0)
        self.assertIn('BROWSE_USERS_METRICS_WARMUP', output)
        self.assertLess(output.index('BROWSE_USERS_METRICS_WARMUP'),
                        output.index('BROWSE_USERS_RESOURCE_GATE_RELEASED'))
        releases = [args for args in commands if args[0] == 'exec']
        self.assertEqual(len(releases), 1)
        self.assertIn('pod/' + runner.JOB + '-pod', releases[0])

    def test_preflight_or_existing_job_never_creates_load_resources(self):
        for fault, preflight in ((None, True), ('existing', False), ('no-metrics', False),
                                 ('hpa', False), ('bad-node', False)):
            code, output, commands = self.scenario(fault, preflight)
            self.assertEqual(code, 0 if preflight else 1)
            self.assertFalse(any(args[0] in ('create', 'apply', 'patch') for args in commands))
            self.assertNotIn('PRIVATE', output)

    def test_resource_stop_or_incomplete_results_never_pass_and_stop_only_generator(self):
        for fault in ('resource-stop', 'invalid', 'rollout', 'lost-create', 'foreign-create'):
            with self.subTest(fault=fault):
                code, output, commands = self.scenario(fault)
                self.assertEqual(code, 1)
                self.assertNotIn('BROWSE_USERS_50_OK', output)
                stops = [args for args in commands if args[:2] == ('patch', 'job')]
                self.assertEqual(bool(stops), fault != 'foreign-create')
                self.assertFalse(any(args[0] in ('delete', 'scale') for args in commands))

    def test_incomplete_wrong_run_or_overbudget_workload_is_rejected(self):
        valid = {'result': 'BROWSE_USERS_WORKLOAD_OK', 'run_id': 'abcdef123456', 'users': 50,
                 'users_with_requests': 50, 'attempts': 1000, 'valid_responses': 1000,
                 'status_counts': {'200': 1000}, 'errors': {}, 'observer_checks': [],
                 'inventory_writes': False, 'availability_before_and_after': 100,
                 'pool_processes_unchanged': True, 'latency_slo_tested': False,
                 'elapsed_seconds': 60.1, 'sampled_peak_workload_http_in_flight': 7,
                 'pools': [{'pod': name, 'pool_max': 4, 'pool_error_counters_unchanged': True,
                            'sampled_peak_size': 2, 'sampled_peak_waiting': 0,
                            'borrows_delta': 500, 'connection_attempts_delta': 1}
                           for name in ('browse-a', 'browse-b')],
                 'latency_ms': {'p50': 100, 'p95': 200, 'p99': 300, 'max': 400}}
        runner.validate(valid, 'abcdef123456')
        for key, value in [('users', 100), ('attempts', 1501), ('users_with_requests', 49),
                           ('elapsed_seconds', 30), ('inventory_writes', True), ('run_id', 'wrong'), ('pools', [])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate(dict(valid, **{key: value}), 'abcdef123456')
