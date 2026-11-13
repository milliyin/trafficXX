"""Queue time versus queue depth, retained HTTP evidence and cooperative stop."""
from collections import Counter
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load('wait_scaling', 'scripts/browse-autoscaling.py')
q = load('wait_queue', 'scripts/browse-queue-comparison.py')
read = load('wait_read', 'scripts/check-read-load.py')
with patch.dict(sys.modules, {'read_probe': read}):
    probe = load('wait_probe', 'scripts/check-browse-autoscaling-load.py')
metrics = load('wait_metrics', 'trafficxx/pool_metrics.py')


class WaitMeasurement(unittest.TestCase):
    def test_driver_counters_render_browse_only_and_cannot_be_negative(self):
        stats = {'requests_queued': 7, 'requests_wait_ms': 84}
        output = metrics.render(stats)
        self.assertIn('trafficxx_browse_pool_queued_total 7\n', output)
        self.assertIn('trafficxx_browse_pool_queue_wait_milliseconds_total 84\n', output)
        self.assertNotIn('queue_wait_milliseconds', metrics.render(stats, 'booking'))
        with self.assertRaises(ValueError):
            metrics.render({'requests_wait_ms': -1})

    def test_mean_is_guarded_by_positive_queue_denominator_and_missing_is_null(self):
        expressions = []
        def query(expression):
            expressions.append(expression)
            if expression.startswith('increase(') and 'queued_total' in expression:
                return [{'metric': {'instance': 'pod:8000'}, 'value': [500, '10']}]
            if expression.startswith('increase(') and 'queue_wait_milliseconds_total' in expression:
                return [{'metric': {'instance': 'pod:8000'}, 'value': [500, '200']}]
            return []
        result = s.queue_wait_diagnostics(query, 400, '440', 460, '500')
        row = result['per_instance']['pod:8000']
        self.assertEqual(row['queue_entries_estimate'], 10)
        self.assertEqual(row['queue_wait_milliseconds_estimate'], 200)
        self.assertIsNone(row['queue_wait_mean_ms'])
        self.assertIsNone(row['sampled_peak_1m_queue_wait_mean_ms'])
        mean = next(expr for expr in expressions if expr.startswith('(increase('))
        self.assertIn('queued_total', mean)
        self.assertIn(' > 0)', mean)
        self.assertNotIn('health_check', ' '.join(expressions))
        self.assertIn('@ 500', mean)
        self.assertIn('@ 440', expressions[-1])

    def test_new_mode_requires_fresh_wait_counters_for_all_warm_pods(self):
        def query(expression):
            value = 32 if expression.startswith(('min(', 'max(')) and 'timestamp' not in expression else \
                    999 if 'timestamp' in expression else 4
            return [{'value': [1000, str(value)]}]
        with patch.object(s, 'WAIT_TIME_MEASUREMENT', True), s.queue_expectation(32), \
             patch.object(s.time, 'time', return_value=1000):
            s.check_queue_metrics(query, 4)
            def missing(expression):
                return [] if 'queue_wait_milliseconds_total' in expression else query(expression)
            with self.assertRaises(ValueError):
                s.check_queue_metrics(missing, 4)

    def test_progress_retains_partial_http_counts_errors_and_recent_latency(self):
        rows = [(200, .04, 'hold'), (200, .05, 'hold'), (503, .08, 'hold')]
        row = probe.progress_record('a' * 12, {'attempts': 3, 'elapsed_seconds': 180}, rows,
                    Counter({'UNEXPECTED_HTTP_STATUS_503': 1}), {0, 1}, 1000,
                    [{'service_error_code': 'DATABASE_BUSY'}], 1)
        self.assertEqual(row['status_counts'], {'200': 2, '503': 1})
        self.assertEqual(row['recent_http']['attempts'], 2)
        self.assertEqual(row['recent_http']['status_counts'], {'200': 1, '503': 1})
        self.assertEqual(row['recent_http']['latency_ms']['max'], 80)
        self.assertEqual(row['users_with_requests'], 2)
        self.assertEqual(row['peak_active_users'], 1000)
        self.assertEqual(row['errors'], {'UNEXPECTED_HTTP_STATUS_503': 1})
        self.assertFalse(row['final_workload_summary_available'])

    def test_external_stop_wakes_paced_users_and_never_reports_a_pass(self):
        stop = threading.Event()
        def http(url, customer):
            stop.set()
            return 200, {'id': 'demo-general', 'inventory_type': 'GENERAL', 'available': 100, 'seats': None}
        with patch.object(probe, 'publish') as publish, redirect_stdout(io.StringIO()):
            row = probe.run('a' * 12, users=1000, duration=420, ramp_down=True,
                            http=http, stop_event=stop)
        self.assertEqual(row['result'], 'BROWSE_SCALING_LOAD_FAILED')
        self.assertTrue(row['stop_requested'])
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['status_counts'], {'200': 1})
        self.assertEqual(row['errors'], {})
        self.assertEqual(publish.call_args.args, ('summary.json', row))

    def test_wait_measured_cli_keeps_old_job_and_uses_retained_mode(self):
        with patch.object(q, 'run') as run, patch.object(q.s, 'DURABLE_EVIDENCE', False), \
             patch.object(q.s, 'WAIT_TIME_MEASUREMENT', False), patch.object(q.s, 'QUEUE32_PROFILE_JOB', 'old'), \
             patch.object(q.argparse.ArgumentParser, 'parse_args',
                          return_value=SimpleNamespace(action='run-wait-measured')):
            self.assertEqual(q.main(), 0)
            run.assert_called_once()
            self.assertTrue(q.s.DURABLE_EVIDENCE)
            self.assertTrue(q.s.WAIT_TIME_MEASUREMENT)
            self.assertEqual(q.s.QUEUE32_PROFILE_JOB, 'browse-users-1000-queue32-waittime')


if __name__ == '__main__':
    unittest.main()
