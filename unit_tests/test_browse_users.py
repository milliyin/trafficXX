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
    def test_single_query_probe_requires_timers_and_reports_interval_deltas(self):
        config = dict(fixtures.config(), users=100, client_memory_comparison=True,
                      single_query_comparison=True, require_warm_pool=True,
                      require_rejection_counters=True, required_queue_limit=16)
        service = Mock()
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'DEPLOY_SINGLE_QUERY_TIMERS_FIRST'):
            probe.run(config, fixtures.Http(), service)
        service.assert_not_called()

        class Timed(fixtures.Http):
            def __call__(self, target, path, customer):
                status, row = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    stats = row['statistics']
                    stats.update(pool_min=4, pool_size=4, pool_available=4, pool_waiting_limit=16,
                                 rejections_queue_full=0, rejections_timeout=0, rejections_closed=0,
                                 timing_event_query_count=0, timing_units_query_count=0)
                    for phase in probe.TIMING_PHASES:
                        stats['timing_' + phase + '_count'] = stats['requests_num']
                        stats['timing_' + phase + '_total_us'] = stats['requests_num'] * 1000
                return status, row
        http, sequence = Timed(), iter(range(100000))
        def service(url, customer):
            return http(config['targets']['browse'][next(sequence) % 2], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=.5, think=.05)
        self.assertTrue(result['single_query_verified'])
        self.assertEqual(result['users_with_requests'], 100)
        self.assertEqual(result['attempts'], result['valid_responses'])
        for pool in result['pools']:
            self.assertEqual(pool['timings']['availability_query']['mean_ms'], 1)
        config['users'] = 250
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=.7, think=.05)
        self.assertTrue(result['single_query_verified'])
        self.assertEqual(result['users_with_requests'], 250)
        self.assertTrue(250 <= result['attempts'] <= 7500)
        self.assertEqual(result['attempts'], result['valid_responses'])
        before = http(config['targets']['browse'][0], '/internal/database-pool', 'test')[1]['statistics']
        changed = dict(before, timing_event_query_count=1)
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'LEGACY_QUERY_COUNTER_INCREASED'):
            probe.query_timing_deltas(before, changed)

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

    def test_hundred_paced_users_require_explicit_stage_and_keep_gets_bounded(self):
        config, http, urls = fixtures.config(), fixtures.Http(), []
        config['users'] = 100
        def service(url, customer):
            self.assertTrue(url.startswith('http://browse.trafficxx.svc.cluster.local/events/demo-'))
            urls.append(url)
            return http(config['targets']['browse'][len(urls) % 2], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=.5, think=.05)
        self.assertEqual(result['users_with_requests'], 100)
        self.assertTrue(100 <= result['attempts'] <= 3000)
        self.assertEqual(result['attempts'], result['valid_responses'])
        self.assertLessEqual(result['sampled_peak_workload_http_in_flight'], 100)
        for count in (True, 250, 1000):
            with self.assertRaises(fixtures.probe.CheckFailed):
                probe.run(dict(config, users=count), http, service, duration=.1, think=.02)
        with self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(fixtures.config(), http, service, users=100)

    def test_warm_comparison_rejects_cold_baseline_and_accepts_full_existing_pool(self):
        config = dict(fixtures.config(), users=100, require_warm_pool=True)
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'WARM_POOL_NOT_READY'):
            probe.run(config, fixtures.Http(), Mock())
        class Warm(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    body['statistics'].update(pool_min=4, pool_size=4, pool_available=4)
                return status, body
        http = Warm()
        def service(url, customer):
            return http(config['targets']['browse'][0], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=.4, think=.02)
        self.assertTrue(result['warm_pool_verified'])
        self.assertEqual(result['users_with_requests'], 100)

    def test_250_probe_requires_policy_before_traffic_and_bounds_attempts(self):
        config = dict(fixtures.config(), users=250, require_warm_pool=True,
                      require_rejection_counters=True, required_queue_limit=16,
                      client_memory_comparison=True)
        service = Mock()
        for key in ('require_warm_pool', 'require_rejection_counters',
                    'required_queue_limit', 'client_memory_comparison'):
            changed = dict(config)
            changed.pop(key)
            with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'LARGER_STAGE_REQUIRES_ALL_GUARDS'):
                probe.run(changed, Mock(), service)
        service.assert_not_called()

        class Warm(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    body['statistics'].update(pool_min=4, pool_size=4, pool_available=4,
                        pool_waiting_limit=16, rejections_queue_full=0,
                        rejections_timeout=0, rejections_closed=0)
                return status, body
        http = Warm()
        def service(url, customer):
            return http(config['targets']['browse'][0], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            result = probe.run(config, http, service, duration=1, think=.05)
        self.assertEqual(result['users_with_requests'], 250)
        self.assertTrue(250 <= result['attempts'] <= 7500)
        self.assertEqual(result['attempts'], result['valid_responses'])
        self.assertFalse(result['inventory_writes'])
        self.assertEqual(result['verified_queue_limit'], 16)
        self.assertEqual(len(result['initial_pools']), 2)

    def test_unexpected_http_response_stops_without_printing_response_body(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(fixtures.config(), fixtures.Http(), lambda *args: (503, {'PRIVATE': 'BODY'}),
                      duration=.3, think=.02)
        self.assertIn('BROWSE_USERS_PHASE_FAILED', output.getvalue())
        self.assertIn('UNEXPECTED_HTTP_STATUS_503', output.getvalue())
        self.assertNotIn('DEMO_INVENTORY_CHANGED', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertIn('UNRECOGNIZED', output.getvalue())

    def test_failure_captures_only_known_service_code_and_same_process_counter_delta(self):
        failed = [False]
        class Counters(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    body['PRIVATE'] = 'DO_NOT_PRINT'
                    body['statistics']['private'] = 'DO_NOT_PRINT'
                    if failed[0] and target['pod'] == 'browse-1':
                        body['statistics']['requests_errors'] += 1
                return status, body
        def service(*args):
            failed[0] = True
            return 503, {'detail': {'code': 'DATABASE_BUSY', 'private': 'DO_NOT_PRINT'}}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(fixtures.config(), Counters(), service, duration=.3, think=.02)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        failure = next(row for row in rows if row['result'] == 'BROWSE_USERS_PHASE_FAILED')
        self.assertEqual(failure['first_failure']['service_error_code'], 'DATABASE_BUSY')
        self.assertEqual(failure['first_failure']['http_status'], 503)
        self.assertGreaterEqual(failure['service_error_codes']['DATABASE_BUSY'], 1)
        pools = failure['pool_snapshots_after_workload_stopped']
        self.assertEqual(pools[1]['before']['statistics']['requests_errors'], 0)
        self.assertEqual(pools[1]['counter_deltas']['requests_errors'], 1)
        self.assertTrue(pools[1]['same_process'])
        self.assertNotIn('DO_NOT_PRINT', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_failure_snapshots_do_not_mask_original_failure_or_compare_changed_process(self):
        config, http = fixtures.config(), fixtures.Http()
        targets = config['targets']['browse']
        initial = {row['pod']: fixtures.probe.pool_snapshot(row, 'test', http) for row in targets}
        with patch.object(probe.read, 'pool_snapshot', side_effect=RuntimeError('PRIVATE')):
            result = probe.failure_pools(targets, initial, 'test', http)
        self.assertEqual(result[0]['snapshot_error_type'], 'RuntimeError')
        self.assertNotIn('PRIVATE', json.dumps(result))
        after = deepcopy(initial[targets[0]['pod']])
        after['instance_id'] = 'a' * 32
        with patch.object(probe.read, 'pool_snapshot', return_value=after):
            result = probe.failure_pools(targets, initial, 'test', http)
        self.assertFalse(result[0]['same_process'])
        self.assertIsNone(result[0]['counter_deltas'])

    def test_generator_memory_reads_only_linux_numeric_fields_and_handles_missing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status'
            self.assertIsNone(probe.generator_memory(path))
            path.write_text('Name: ignored\nVmRSS: 65536 kB\nThreads: 102\n')
            self.assertEqual(probe.generator_memory(path), {'rss_mib': 64, 'threads': 102})
            path.write_text('VmRSS: INVALID kB\nThreads: 102\n')
            self.assertIsNone(probe.generator_memory(path))

    def test_rejection_diagnostic_requires_new_counters_and_captures_per_type_delta(self):
        config = dict(fixtures.config(), users=100, require_rejection_counters=True)
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'DEPLOY_POOL_REJECTION_COUNTERS_FIRST'):
            probe.run(config, fixtures.Http(), Mock())
        failed = [False]
        class Diagnostic(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    body['statistics'].update(rejections_queue_full=int(failed[0]),
                                              rejections_timeout=0, rejections_closed=0)
                return status, body
        def service(*args):
            failed[0] = True
            return 503, {'detail': {'code': 'DATABASE_BUSY'}}
        output = io.StringIO()
        with patch.object(probe, 'generator_memory', return_value={'rss_mib': 160, 'threads': 102}), \
                redirect_stdout(output), self.assertRaises(fixtures.probe.CheckFailed):
            probe.run(config, Diagnostic(), service, duration=.3, think=.02)
        failure = next(json.loads(line) for line in output.getvalue().splitlines()
                       if json.loads(line)['result'] == 'BROWSE_USERS_PHASE_FAILED')
        self.assertEqual(failure['pool_snapshots_after_workload_stopped'][0]['counter_deltas']['rejections_queue_full'], 1)
        self.assertEqual(failure['generator_memory']['sampled_max_rss_mib'], 160)
        self.assertEqual(failure['generator_memory']['sampled_max_threads'], 102)

    def test_queue_comparison_checks_actual_limit_and_accepts_bounded_sixteen(self):
        config = dict(fixtures.config(), users=100, required_queue_limit=16)
        service = Mock()
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'DEPLOY_REQUESTED_QUEUE_LIMIT_FIRST'):
            probe.run(config, fixtures.Http(), service)
        service.assert_not_called()
        class Queue(fixtures.Http):
            def __call__(self, target, path, customer):
                status, body = super().__call__(target, path, customer)
                if path == '/internal/database-pool':
                    body['statistics']['pool_waiting_limit'] = 16
                return status, body
        http = Queue()
        def service(url, customer):
            return http(config['targets']['browse'][0], '/' + url.split('/', 3)[3], customer)
        with redirect_stdout(io.StringIO()):
            outcome = probe.run(config, http, service, duration=.4, think=.02)
        self.assertEqual(outcome['verified_queue_limit'], 16)
        self.assertTrue(all(row['pool_waiting_limit'] == 16 for row in outcome['pools']))
        target = config['targets']['browse'][0]
        def snapshot(*args):
            status, row = http(*args)
            row['statistics']['requests_waiting'] = 17
            return status, row
        with self.assertRaisesRegex(fixtures.probe.CheckFailed, 'POOL_BOUNDS_CHANGED'):
            fixtures.probe.pool_snapshot(target, 'test', snapshot)

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
    def scenario(self, fault=None, preflight_only=False, users=50, diagnostic_retry=False, warm_pool=False, rejection_diagnostics=False, queue_comparison=False, client_memory_comparison=False, single_query_comparison=False):
        commands, polls = [], [0]
        if single_query_comparison:
            client_memory_comparison = True
        if users == 250:
            client_memory_comparison = True
        if client_memory_comparison:
            queue_comparison = True
        if queue_comparison:
            warm_pool = rejection_diagnostics = True
        job_name = (f'browse-users-{users}-singlequery' if single_query_comparison else
                    'browse-users-250' if users == 250 else
                    'browse-users-100-clientmemory' if client_memory_comparison else
                    'browse-users-100-queue16' if queue_comparison else
                    'browse-users-100-rejections' if rejection_diagnostics else
                    'browse-users-100-warm' if warm_pool else
                    'browse-users-100-diagnostic' if diagnostic_retry else
                    runner.JOB if users == 50 else 'browse-users-100')
        generator = job_name + '-pod'
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
                return json.dumps({'items': [{'metadata': {'name': 'browse-users-50-v3'}, 'status': {}}]
                                  if fault == 'active-earlier-stage' else []})
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
                    {'name': 'browse', 'env': ([{'name': 'BROWSE_DB_POOL_MIN', 'value': '4'},
                        {'name': 'BROWSE_DB_POOL_MAX', 'value': '4'},
                        {'name': 'BROWSE_DB_POOL_MAX_WAITING', 'value': '16' if queue_comparison and fault != 'old-queue' else '8'}] if warm_pool and fault != 'cold-policy' else []), 'resources': {'limits': {'cpu': '500m', 'memory': '256Mi'}}}]}}}})
            if args[:2] == ('create', 'configmap'):
                source = next(arg.split('=', 2)[2] for arg in args if arg.startswith('--from-file=targets.json='))
                self.assertEqual(json.loads(Path(source).read_text())['users'], users)
                self.assertEqual(json.loads(Path(source).read_text())['require_warm_pool'], warm_pool)
                self.assertEqual(json.loads(Path(source).read_text())['require_rejection_counters'], rejection_diagnostics)
                self.assertEqual(json.loads(Path(source).read_text())['required_queue_limit'], 16 if queue_comparison else 8)
                self.assertEqual(json.loads(Path(source).read_text())['client_memory_comparison'], client_memory_comparison)
                self.assertEqual(json.loads(Path(source).read_text())['single_query_comparison'], single_query_comparison)
                return '{}'
            if args[:2] == ('patch', '--local'):
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
            result = runner.execute(preflight_only=preflight_only, users=users, diagnostic_retry=diagnostic_retry, warm_pool=warm_pool,
                                    rejection_diagnostics=rejection_diagnostics, queue_comparison=queue_comparison,
                                    client_memory_comparison=client_memory_comparison,
                                    single_query_comparison=single_query_comparison)
        return result, output.getvalue(), commands

    def test_success_requires_resource_and_job_evidence_and_unchanged_pods(self):
        code, output, commands = self.scenario()
        self.assertEqual(code, 0)
        self.assertIn('BROWSE_USERS_50_OK', output)
        self.assertFalse(any(args[0] in ('delete', 'scale') for args in commands))

    def test_hundred_stage_owns_separate_job_and_blocks_overlapping_fifty(self):
        code, output, commands = self.scenario(users=100)
        self.assertEqual(code, 0)
        self.assertIn('BROWSE_USERS_100_OK', output)
        self.assertIn('"max_workload_attempts": 3000', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-pod' in args for args in commands))
        render = next(args for args in commands if args[:2] == ('patch', '--local'))
        operations = json.loads(render[render.index('--patch') + 1])
        self.assertIn({'op': 'replace', 'path': '/metadata/name', 'value': 'browse-users-100'}, operations)
        code, output, commands = self.scenario('resource-stop', users=100)
        stops = [args for args in commands if args[:2] == ('patch', 'job')]
        self.assertEqual(code, 1)
        self.assertEqual(stops[0][2], 'browse-users-100')
        code, output, commands = self.scenario('active-earlier-stage', users=100)
        self.assertEqual(code, 1)
        self.assertIn('OTHER_READ_LOAD_ACTIVE', output)
        self.assertFalse(any(args[0] in ('create', 'apply', 'patch') for args in commands))

    def test_diagnostic_retry_is_explicit_separate_and_stops_only_its_owned_job(self):
        code, output, commands = self.scenario(users=100, diagnostic_retry=True)
        self.assertEqual(code, 0)
        self.assertIn('"job": "browse-users-100-diagnostic"', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-diagnostic-pod' in args
                            for args in commands))
        code, output, commands = self.scenario('resource-stop', users=100, diagnostic_retry=True)
        self.assertEqual(code, 1)
        stops = [args for args in commands if args[:2] == ('patch', 'job')]
        self.assertEqual(stops[0][2], 'browse-users-100-diagnostic')
        code, output, commands = self.scenario('active-earlier-stage', users=100, diagnostic_retry=True)
        self.assertEqual(code, 1)
        self.assertFalse(any(args[0] in ('create', 'apply', 'patch') for args in commands))
        with self.assertRaisesRegex(ValueError, 'DIAGNOSTIC_RETRY_REQUIRES_100_USERS'):
            runner.execute(users=50, diagnostic_retry=True)

    def test_warm_comparison_requires_deployed_policy_and_separate_owned_job(self):
        code, output, commands = self.scenario(users=100, warm_pool=True)
        self.assertEqual(code, 0)
        self.assertIn('"job": "browse-users-100-warm"', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-warm-pod' in args for args in commands))
        code, output, commands = self.scenario('cold-policy', users=100, warm_pool=True)
        self.assertEqual(code, 1)
        self.assertIn('DEPLOY_WARM_BROWSE_POOL_FIRST', output)
        self.assertFalse(any(args[0] in ('create', 'apply', 'patch') for args in commands))
        with self.assertRaisesRegex(ValueError, 'SELECT_ONE_COMPARISON'):
            runner.execute(users=100, warm_pool=True, diagnostic_retry=True)

    def test_rejection_diagnostic_selects_own_job_and_requires_warm_mode(self):
        code, output, commands = self.scenario(users=100, warm_pool=True, rejection_diagnostics=True)
        self.assertEqual(code, 0)
        self.assertIn('"job": "browse-users-100-rejections"', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-rejections-pod' in args for args in commands))
        with self.assertRaisesRegex(ValueError, 'REJECTION_DIAGNOSTICS_REQUIRES_WARM_COMPARISON'):
            runner.execute(users=100, rejection_diagnostics=True)

    def test_queue_comparison_requires_deployed_sixteen_and_owns_new_job(self):
        code, output, commands = self.scenario(users=100, queue_comparison=True)
        self.assertEqual(code, 0)
        self.assertIn('"job": "browse-users-100-queue16"', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-queue16-pod' in args for args in commands))
        code, output, commands = self.scenario('old-queue', users=100, queue_comparison=True)
        self.assertEqual(code, 1)
        self.assertIn('DEPLOY_QUEUE_COMPARISON_FIRST', output)
        self.assertFalse(any(args[0] in ('create', 'apply', 'patch') for args in commands))
        code, output, commands = self.scenario('resource-stop', users=100, queue_comparison=True)
        self.assertEqual(code, 1)
        self.assertEqual(next(args for args in commands if args[:2] == ('patch', 'job'))[2], 'browse-users-100-queue16')
        with self.assertRaisesRegex(ValueError, 'QUEUE_COMPARISON_REQUIRES_100_USERS'):
            runner.execute(users=50, queue_comparison=True)

    def test_client_memory_comparison_has_own_job_and_keeps_existing_pool_policy(self):
        code, output, commands = self.scenario(users=100, client_memory_comparison=True)
        self.assertEqual(code, 0)
        self.assertIn('"job": "browse-users-100-clientmemory"', output)
        self.assertIn('"queue_comparison": true', output)
        self.assertTrue(any(args[0] == 'exec' and 'pod/browse-users-100-clientmemory-pod' in args for args in commands))
        code, output, commands = self.scenario('resource-stop', users=100, client_memory_comparison=True)
        self.assertEqual(code, 1)
        self.assertEqual(next(args for args in commands if args[:2] == ('patch', 'job'))[2], 'browse-users-100-clientmemory')
        with self.assertRaisesRegex(ValueError, 'QUEUE_COMPARISON_REQUIRES_100_USERS'):
            runner.execute(users=50, client_memory_comparison=True)

    def test_250_stage_forces_guards_and_stops_only_its_own_job(self):
        code, output, commands = self.scenario(users=250)
        self.assertEqual(code, 0, output)
        self.assertIn('BROWSE_USERS_250_OK', output)
        self.assertIn('"max_workload_attempts": 7500', output)
        self.assertIn('"job": "browse-users-250"', output)
        code, output, commands = self.scenario('resource-stop', users=250)
        self.assertEqual(code, 1)
        self.assertEqual(next(args for args in commands if args[:2] == ('patch', 'job'))[2], 'browse-users-250')
        for fault in ('old-queue', 'cold-policy', 'active-earlier-stage'):
            code, output, commands = self.scenario(fault, users=250)
            self.assertEqual(code, 1, output)
            self.assertNotIn('BROWSE_USERS_250_OK', output)
        with self.assertRaises(ValueError):
            runner.execute(users=250, diagnostic_retry=True)

    def test_single_query_stage_owns_job_and_preserves_existing_guards(self):
        code, output, commands = self.scenario(users=100, single_query_comparison=True)
        self.assertEqual(code, 0, output)
        self.assertIn('"job": "browse-users-100-singlequery"', output)
        self.assertIn('"max_workload_attempts": 3000', output)
        code, output, commands = self.scenario('resource-stop', users=100, single_query_comparison=True)
        self.assertEqual(code, 1)
        self.assertEqual(next(args for args in commands if args[:2] == ('patch', 'job'))[2],
                         'browse-users-100-singlequery')
        for fault in ('active-earlier-stage', 'old-queue', 'cold-policy'):
            code, output, commands = self.scenario(fault, users=100, single_query_comparison=True)
            self.assertEqual(code, 1, output)
        for count in (50,):
            with self.assertRaisesRegex(ValueError, 'SINGLE_QUERY_COMPARISON_REQUIRES_100_OR_250_USERS'):
                runner.execute(users=count, single_query_comparison=True)

    def test_250_single_query_stage_keeps_failed_stage_separate_and_stop_scoped(self):
        code, output, commands = self.scenario(users=250, single_query_comparison=True)
        self.assertEqual(code, 0, output)
        self.assertIn('"job": "browse-users-250-singlequery"', output)
        self.assertIn('"max_workload_attempts": 7500', output)
        self.assertIn('BROWSE_USERS_250_OK', output)
        self.assertFalse(any(args[0] in ('delete', 'scale') for args in commands))
        for fault in ('resource-stop', 'active-earlier-stage', 'old-queue', 'cold-policy', 'existing'):
            code, output, commands = self.scenario(fault, users=250, single_query_comparison=True)
            self.assertEqual(code, 1, output)
            for args in commands:
                if args[:2] == ('patch', 'job'):
                    self.assertEqual(args[2], 'browse-users-250-singlequery')
        with self.assertRaises(ValueError):
            runner.execute(users=250, single_query_comparison=True, diagnostic_retry=True)

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
        hundred = dict(valid, users=100, users_with_requests=100, attempts=3000,
                       valid_responses=3000, status_counts={'200': 3000},
                       sampled_peak_workload_http_in_flight=100)
        runner.validate(hundred, 'abcdef123456', 100)
        with self.assertRaisesRegex(ValueError, 'WARM_POOL_NOT_VERIFIED'):
            runner.validate(hundred, 'abcdef123456', 100, True)
        warm = dict(hundred, warm_pool_verified=True, initial_pools={name: {
                    'statistics': {'pool_min': 4, 'pool_max': 4, 'pool_size': 4}}
                    for name in ('browse-a', 'browse-b')})
        runner.validate(warm, 'abcdef123456', 100, True)
        with self.assertRaisesRegex(ValueError, 'REJECTION_COUNTERS_NOT_VERIFIED'):
            runner.validate(warm, 'abcdef123456', 100, True, True)
        warm['rejection_counters_verified'] = True
        for row in warm['initial_pools'].values():
            row['statistics'].update(rejections_queue_full=0, rejections_timeout=0, rejections_closed=0)
        runner.validate(warm, 'abcdef123456', 100, True, True)
        queue = deepcopy(warm)
        queue['verified_queue_limit'] = 16
        for row in queue['initial_pools'].values():
            row['statistics']['pool_waiting_limit'] = 16
        for row in queue['pools']:
            row.update(pool_waiting_limit=16, sampled_peak_waiting=12)
        runner.validate(queue, 'abcdef123456', 100, True, True, True)
        with self.assertRaisesRegex(ValueError, 'CLIENT_POLICY_NOT_VERIFIED'):
            runner.validate(queue, 'abcdef123456', 100, True, True, True, True)
        queue['http_client_policy'] = 'thread-local-http-only-fresh-connections-v1'
        with self.assertRaisesRegex(ValueError, 'CLIENT_MEMORY_EVIDENCE_MISSING'):
            runner.validate(queue, 'abcdef123456', 100, True, True, True, True)
        queue['generator_memory'] = {'source': '/proc/self/status', 'samples': 3,
                                     'sampled_max_rss_mib': 50, 'sampled_max_threads': 102}
        runner.validate(queue, 'abcdef123456', 100, True, True, True, True)
        with self.assertRaisesRegex(ValueError, 'SINGLE_QUERY_NOT_VERIFIED'):
            runner.validate(queue, 'abcdef123456', 100, True, True, True, True, True)
        single = deepcopy(queue)
        single['single_query_verified'] = True
        with self.assertRaisesRegex(ValueError, 'SINGLE_QUERY_TIMINGS_INCOMPLETE'):
            runner.validate(single, 'abcdef123456', 100, True, True, True, True, True)
        for row in single['pools']:
            row['timings'] = {phase: {'observations': 100, 'total_us': 100000, 'mean_ms': 1}
                              for phase in probe.TIMING_PHASES}
        runner.validate(single, 'abcdef123456', 100, True, True, True, True, True)
        single_250 = dict(single, users=250, users_with_requests=250, attempts=7500,
                          valid_responses=7500, status_counts={'200': 7500})
        runner.validate(single_250, 'abcdef123456', 250, True, True, True, True, True)
        with self.assertRaises(ValueError):
            runner.validate(dict(single_250, attempts=7501), 'abcdef123456', 250, True, True, True, True, True)
        larger = dict(queue, users=250, users_with_requests=250, attempts=7500,
                      valid_responses=7500, status_counts={'200': 7500})
        runner.validate(larger, 'abcdef123456', 250, True, True, True, True)
        with self.assertRaisesRegex(ValueError, 'LARGER_STAGE_REQUIRES_ALL_GUARDS'):
            runner.validate(larger, 'abcdef123456', 250)
        with self.assertRaises(ValueError):
            runner.validate(dict(larger, attempts=7501), 'abcdef123456', 250, True, True, True, True)
        with self.assertRaises(ValueError):
            runner.validate(queue, 'abcdef123456', 100, True, True)
        queue['verified_queue_limit'] = 8
        with self.assertRaisesRegex(ValueError, 'QUEUE_LIMIT_NOT_VERIFIED'):
            runner.validate(queue, 'abcdef123456', 100, True, True, True)
        warm['initial_pools']['browse-a']['statistics']['pool_min'] = 1
        with self.assertRaisesRegex(ValueError, 'WARM_POOL_NOT_VERIFIED'):
            runner.validate(warm, 'abcdef123456', 100, True)
        for changed in (dict(hundred, attempts=3001), dict(hundred, users_with_requests=99),
                        dict(hundred, users=50), dict(hundred, sampled_peak_workload_http_in_flight=101)):
            with self.assertRaises(ValueError):
                runner.validate(changed, 'abcdef123456', 100)
        for key, value in [('users', 100), ('attempts', 1501), ('users_with_requests', 49),
                           ('elapsed_seconds', 30), ('inventory_writes', True), ('run_id', 'wrong'), ('pools', [])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.validate(dict(valid, **{key: value}), 'abcdef123456')
