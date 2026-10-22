"""Scrape evidence decisions using fake Prometheus API responses."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock
import urllib.parse

spec = importlib.util.spec_from_file_location(
    'prometheus_http', Path(__file__).resolve().parents[1] / 'scripts/check-prometheus-http.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PrometheusHttp(unittest.TestCase):
    def setUp(self):
        self.samples = dict(up=1, freshness_seconds=2, samples_in_two_minutes=3,
                            processed_total=18, processing_outstanding=0, outbox_pending=0,
                            active_alerts=0, historical_dead_letters=1)
        self.targets = [dict(labels={'job': module.JOB}, health='up', lastError='',
                             scrapeUrl='http://operations.trafficxx.svc.cluster.local:80/metrics')]

    def get(self, path, text=False):
        if path.startswith('/-/'):
            return 'ready'
        if path == '/api/v1/targets':
            return {'status': 'success', 'data': {'activeTargets': self.targets}}
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['query'][0]
        key = next(k for k, v in module.QUERIES.items() if v == query)
        return {'status': 'success', 'data': {'resultType': 'vector',
                'result': [{'metric': {'job': module.JOB}, 'value': [123, str(self.samples[key])]}]}}

    def test_healthy_stored_samples_pass_and_history_is_not_live_failure(self):
        result = module.verify(self.get)
        self.assertEqual(result['result'], 'PROMETHEUS_SCRAPE_OK')
        self.assertEqual(result['samples']['samples_in_two_minutes'], 3)
        self.assertFalse(result['native_queue_counts_available'])
        self.assertFalse(result['notification_delivery_configured'])

    def test_missing_down_wrong_or_duplicate_target_fails(self):
        for targets in ([], [dict(self.targets[0], health='down')], self.targets * 2,
                        [dict(self.targets[0], scrapeUrl='http://other/metrics')],
                        [dict(self.targets[0], lastError='DO_NOT_LOG_RAW')]):
            with self.subTest(targets=targets):
                self.targets = targets
                with self.assertRaises(ValueError):
                    module.verify(self.get)
                self.setUp()

    def test_stale_or_single_scrape_cannot_prove_fresh_history(self):
        for key, value in [('freshness_seconds', 46), ('freshness_seconds', -1),
                           ('samples_in_two_minutes', 1), ('up', 0), ('processed_total', 0)]:
            with self.subTest(key=key, value=value):
                self.setUp()
                self.samples[key] = value
                with self.assertRaises(ValueError):
                    module.verify(self.get)

    def test_backlog_or_active_alert_fails_healthy_baseline(self):
        for key in ('processing_outstanding', 'outbox_pending', 'active_alerts'):
            with self.subTest(key=key):
                self.setUp()
                self.samples[key] = 1
                with self.assertRaises(ValueError):
                    module.verify(self.get)

    def test_nonfinite_or_multiple_series_cannot_pass(self):
        for value in ('NaN', '+Inf', '-Inf'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.scalar({'status': 'success', 'data': {'resultType': 'vector',
                              'result': [{'value': [0, value]}]}})
        with self.assertRaises(ValueError):
            module.scalar({'status': 'success', 'data': {'resultType': 'vector', 'result': []}})

    def test_wait_retries_until_two_samples_exist(self):
        calls = [0]
        sleep = MagicMock()

        def get(path, text=False):
            if path == '/-/healthy':
                calls[0] += 1
            self.samples['samples_in_two_minutes'] = calls[0]
            return self.get(path, text)
        self.assertEqual(module.wait(get, sleep)['result'], 'PROMETHEUS_SCRAPE_OK')
        sleep.assert_called_once_with(5)

    def test_failed_wait_has_bounded_attempts(self):
        self.samples['up'] = 0
        sleep = MagicMock()
        with self.assertRaises(ValueError):
            module.wait(self.get, sleep)
        self.assertEqual(sleep.call_count, 23)
