"""Bounded read workload decisions and cloud-runner evidence, without Azure."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('read_load_probe', 'check-read-load.py')
runner = load('read_load_runner', 'run-read-load.py')


class Http:
    def __init__(self, fault=None):
        self.fault = fault
        self.lock = threading.Lock()
        self.counts, self.snapshots = {}, {}
        self.calls = []

    def __call__(self, target, path, customer):
        with self.lock:
            name = target['pod']
            self.calls.append((target['api'], path, customer))
            self.counts.setdefault(name, 0)
            self.snapshots.setdefault(name, 0)
            if path == '/internal/database-pool':
                self.snapshots[name] += 1
                return 200, {'instance_id': ('b' if target['api'] == 'browse' else 'c') * 32,
                             'statistics': {'pool_min': 1, 'pool_max': 4, 'pool_size': 1,
                             'pool_available': 1, 'requests_waiting': 0, 'requests_num': self.counts[name],
                             'connections_num': 1, 'requests_errors': int(
                                 self.fault == 'observer' and self.snapshots[name] > 1),
                             'connections_errors': 0, 'connections_lost': 0, 'returns_bad': 0}}
            self.counts[name] += 1
            if self.fault == 'http':
                return 503, {'private': 'DO_NOT_PRINT_BODY'}
            if target['api'] == 'booking':
                return 404, {'detail': {'code': 'RESERVATION_NOT_FOUND'}}
            seated = 'seated' in path
            return 200, {'id': 'demo-seated' if seated else 'demo-general',
                         'available': 99 if self.fault == 'inventory' else 100,
                         'inventory_type': 'SEATED' if seated else 'GENERAL',
                         'seats': [f'A{n}' for n in range(1, 101)] if seated else None}


def config():
    return {'run_id': 'abcdef123456', 'targets': {api: [
        {'api': api, 'pod': api + '-' + str(n), 'ip': '10.244.' + str(index) + '.' + str(n + 1)}
        for n in range(2)] for index, api in enumerate(('browse', 'booking'))}}


class ReadLoad(unittest.TestCase):
    def test_nearest_rank_percentiles_are_explicit_and_empty_is_not_a_measurement(self):
        self.assertEqual(probe.percentiles([.004, .001, .002, .003]),
                         {'p50': 2, 'p95': 4, 'p99': 4, 'max': 4})
        with self.assertRaises(probe.CheckFailed):
            probe.percentiles([])

    def test_nonpod_duplicate_or_wrong_component_targets_fail_before_load(self):
        for fault in ('external', 'duplicate', 'wrong-api'):
            data = config()
            if fault == 'external':
                data['targets']['browse'][0]['ip'] = '8.8.8.8'
            elif fault == 'duplicate':
                data['targets']['browse'][1]['ip'] = data['targets']['browse'][0]['ip']
            else:
                data['targets']['browse'][0]['api'] = 'booking'
            with self.subTest(fault=fault), self.assertRaises(probe.CheckFailed):
                probe.targets_from(data)

    def test_bounded_workload_counts_expected404_as_success_and_preserves_inventory(self):
        http = Http()
        with redirect_stdout(io.StringIO()):
            result = probe.run(config(), http, duration=.12, period=.01)
        self.assertEqual(result['result'], 'READ_LOAD_WORKLOAD_OK')
        self.assertFalse(result['inventory_writes'])
        self.assertFalse(result['latency_slo_tested'])
        for workers, phase in zip((1, 4), result['phases']):
            self.assertEqual(phase['max_workload_http_concurrency'], workers * 2)
            self.assertLessEqual(sum(row['attempts'] for row in phase['apis'].values()), workers * 2 * 12)
            self.assertTrue(all(row['valid_responses'] == row['attempts'] for row in phase['apis'].values()))
            self.assertEqual(set(phase['apis']['booking']['status_counts']), {'404'})
            self.assertTrue(all(row['pool_error_counters_unchanged'] for row in phase['pools']))
            self.assertTrue(all(row['connection_attempts_delta'] == 0 for row in phase['pools']))
        self.assertTrue(all(path.startswith(('/events/', '/reservations/', '/internal/'))
                            for _, path, _ in http.calls))

    def test_http_failure_stops_phase_and_reports_status_without_response_body(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(probe.CheckFailed):
            probe.phase(config()['targets'], 'lab-customer', 'missing', 4, Http('http'), duration=.2, period=.01)
        self.assertIn('503', output.getvalue())
        self.assertIn('READ_LOAD_PHASE_FAILED', output.getvalue())
        self.assertNotIn('DO_NOT_PRINT_BODY', output.getvalue())

    def test_new_pool_errors_abort_and_changed_inventory_is_not_silently_accepted(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(probe.CheckFailed):
            probe.phase(config()['targets'], 'lab-customer', 'missing', 1, Http('observer'), duration=.2, period=.01)
        with redirect_stdout(io.StringIO()), self.assertRaises(probe.CheckFailed):
            probe.run(config(), Http('inventory'), duration=.12, period=.01)

    def test_transport_uses_get_no_body_no_proxy_and_accepts_expected_http_error(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError('http://test', 404, 'missing', {},
            io.BytesIO(b'{"detail":{"code":"RESERVATION_NOT_FOUND"}}'))
        with patch.object(probe, '_client_state', threading.local()), \
                patch.object(probe, 'build_http_opener', return_value=opener) as build:
            status, body = probe.read_json({'ip': '10.244.0.1'}, '/reservations/missing', 'lab-customer')
        self.assertEqual(status, 404)
        self.assertEqual(body, {'detail': {'code': 'RESERVATION_NOT_FOUND'}})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        build.assert_called_once()


    def test_http_only_opener_does_not_construct_tls_or_enable_extra_protocols(self):
        with patch.object(probe.urllib.request, 'HTTPSHandler', side_effect=AssertionError('TLS_NOT_NEEDED')), \
                patch.object(probe.urllib.request, 'getproxies', side_effect=AssertionError('NO_ENV_PROXY')):
            opener = probe.build_http_opener()
        self.assertTrue(any(isinstance(handler, probe.NoRedirect) for handler in opener.handlers))
        self.assertTrue(any(isinstance(handler, probe.urllib.request.HTTPHandler) for handler in opener.handlers))
        self.assertFalse(hasattr(opener, 'https_open'))
        for handler in opener.handlers:
            self.assertFalse(isinstance(handler, (probe.urllib.request.FileHandler, probe.urllib.request.FTPHandler,
                                                  probe.urllib.request.HTTPCookieProcessor)))
        with patch.object(probe, 'http_opener') as client:
            for url in ('https://example.invalid', 'file:///test', 'ftp://example.invalid', 'data:text/plain,test'):
                with self.assertRaisesRegex(probe.CheckFailed, 'HTTP_ONLY_READ_CLIENT'):
                    probe.read_url(url, 'customer')
            client.assert_not_called()

    def test_http_handlers_are_reused_within_thread_without_reusing_request_headers(self):
        opener = MagicMock()
        response = opener.open.return_value
        response.code, response.read.return_value = 200, b'{"available": 100}'
        with patch.object(probe, '_client_state', threading.local()), \
                patch.object(probe, 'build_http_opener', return_value=opener) as build:
            first = probe.read_url('http://127.0.0.1/one', 'customer-one')
            second = probe.read_url('http://127.0.0.1/two', 'customer-two')
        self.assertEqual(first, second)
        build.assert_called_once()
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertIsNot(requests[0], requests[1])
        self.assertEqual(requests[0].get_header('X-customer-id'), 'customer-one')
        self.assertEqual(requests[1].get_header('X-customer-id'), 'customer-two')
        self.assertEqual(opener.open.return_value.__exit__.call_count, 2)
        self.assertTrue(all(request.data is None and request.get_method() == 'GET' for request in requests))

    def test_http_handler_instances_are_not_shared_between_worker_threads(self):
        barrier = threading.Barrier(2)
        def worker(index):
            first = probe.http_opener()
            barrier.wait(timeout=5)
            return first, probe.http_opener()
        import concurrent.futures
        with patch.object(probe, '_client_state', threading.local()), \
                patch.object(probe, 'build_http_opener', side_effect=lambda *args: object()) as build:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                values = list(executor.map(worker, range(2)))
        self.assertIs(values[0][0], values[0][1])
        self.assertIs(values[1][0], values[1][1])
        self.assertIsNot(values[0][0], values[1][0])
        self.assertEqual(build.call_count, 2)

    def test_reused_handler_closes_oversized_response_and_keeps_size_guard(self):
        opener = MagicMock()
        opener.open.return_value.read.return_value = b'x' * 65537
        with patch.object(probe, '_client_state', threading.local()), \
                patch.object(probe, 'build_http_opener', return_value=opener), \
                self.assertRaisesRegex(probe.CheckFailed, 'RESPONSE_TOO_LARGE'):
            probe.read_url('http://127.0.0.1/test', 'customer')
        opener.open.return_value.__exit__.assert_called_once()


class ReadLoadRunner(unittest.TestCase):
    def scenario(self, fault=None):
        commands = []
        counts = {'browse': 0, 'booking': 0}

        def snapshot(flow, api):
            counts[api] += 1
            data = {'generation': 1, 'pods': [dict(row, image=
                'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'a' * 64, uid='uid', restarts=0)
                for row in config()['targets'][api]]}
            if fault == 'postflight' and api == 'booking' and counts[api] == 2:
                data['pods'][0]['restarts'] = 1
            return data

        def kube(*args, data=None, timeout=30):
            commands.append(args)
            if args == ('config', 'current-context'):
                return 'trafficxx-lab-aks'
            if args[:2] == ('get', 'job') and '--ignore-not-found' in args:
                return 'job/read-load-baseline' if fault == 'existing' else ''
            if args[:2] == ('create', 'configmap'):
                return '{}'
            if args[:2] == ('patch', '--local'):
                return '{"kind":"Job"}'
            if args[:2] == ('create', '-f') and '-o' in args:
                return '{"metadata":{"uid":"job-uid"}}'
            if args[0] == 'wait' and fault == 'wait':
                raise subprocess.CalledProcessError(1, 'kubectl', stderr='PRIVATE_RAW_ERROR')
            if args[0] == 'logs':
                return json.dumps({'result': 'READ_LOAD_WORKLOAD_OK', 'run_id': 'abcdef123456',
                    'inventory_writes': False, 'availability_before_and_after': 100,
                    'pool_processes_unchanged': True, 'latency_slo_tested': False,
                    'phases': [] if fault == 'incomplete' else [
                        {'workers_per_api': workers, 'scheduled_duration_seconds': 30, 'apis': {
                            api: {'attempts': 10, 'valid_responses': 10, 'errors': {},
                                  'status_counts': {status: 10},
                                  'latency_ms': {'p50': 5, 'p95': 8, 'p99': 9, 'max': 10}}
                            for api, status in [('browse', '200'), ('booking', '404')]}}
                        for workers in (1, 4)]})
            if args[:2] == ('get', 'job'):
                return json.dumps({'metadata': {'uid': 'other' if fault == 'job-replaced' else 'job-uid'},
                                   'status': {'succeeded': 1}})
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [{'metadata': {'ownerReferences': [
                    {'controller': True, 'kind': 'Job', 'uid': 'job-uid'}]},
                    'status': {'phase': 'Succeeded', 'containerStatuses': [{'restartCount': 0}]}}]})
            return '{}'

        output = io.StringIO()
        with patch.object(runner, 'command', return_value='35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'), \
                patch.object(runner, 'kube', side_effect=kube), patch.object(runner, 'snapshot', side_effect=snapshot), \
                patch.object(runner, 'uuid4', return_value=type('Id', (), {'hex': 'abcdef123456'})()), \
                redirect_stdout(output):
            status = runner.execute()
        return status, output.getvalue(), commands

    def test_runner_requires_successful_job_and_unchanged_serving_pods(self):
        status, output, commands = self.scenario()
        self.assertEqual(status, 0)
        self.assertIn('READ_LOAD_BASELINE_OK', output)
        self.assertIn('"serving_pods_unchanged": true', output)
        self.assertFalse(any('delete' in command or 'scale' in command for command in commands))

    def test_existing_job_stops_before_creating_or_updating_anything(self):
        status, output, commands = self.scenario('existing')
        self.assertEqual(status, 1)
        self.assertNotIn('READ_LOAD_BASELINE_OK', output)
        self.assertFalse(any(command[0] in ('apply', 'create', 'patch') for command in commands))

    def test_serving_restart_prevents_a_false_final_pass(self):
        for fault, stage in [('postflight', 'postflight'), ('job-replaced', 'validate_job'),
                             ('incomplete', 'validate_job')]:
            with self.subTest(fault=fault):
                status, output, _ = self.scenario(fault)
                self.assertEqual(status, 1)
                self.assertIn('"stage": "' + stage + '"', output)
                self.assertNotIn('READ_LOAD_BASELINE_OK', output)

    def test_wait_failure_retains_job_names_and_logs_without_raw_command_error(self):
        status, output, commands = self.scenario('wait')
        self.assertEqual(status, 1)
        self.assertIn('read-load-baseline', output)
        self.assertIn('"stage": "wait_job"', output)
        self.assertNotIn('PRIVATE_RAW_ERROR', output)
        self.assertFalse(any(command[0] == 'delete' for command in commands))
