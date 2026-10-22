"""Healthy-baseline checker decisions; no actual network or cloud calls."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from test_monitoring import VALUES, ROWS, EXPECTED
from trafficxx.monitoring import evaluate, render_metrics

spec = importlib.util.spec_from_file_location(
    'operations_http', Path(__file__).resolve().parents[1] / 'scripts/check-operations-http.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OperationsHttp(unittest.TestCase):
    def setUp(self):
        self.state = evaluate(deepcopy(VALUES), deepcopy(ROWS), 30, EXPECTED)
        self.text = None
        self.health, self.ready = {'status': 'alive'}, {'status': 'ready'}

    def get(self, path, text=False):
        return {'/healthz': self.health, '/readyz': self.ready, '/backlog': self.state,
                '/metrics': self.text if self.text is not None else render_metrics(self.state)}[path]

    def test_healthy_with_retained_history_passes(self):
        result = module.verify(self.get)
        self.assertEqual(result['result'], 'OPERATIONS_HTTP_OK')
        self.assertEqual(result['historical_observations'], ['DEAD_LETTERS_PREVIOUSLY_OBSERVED'])
        self.assertTrue(result['worker_and_alert_metrics_verified'])
        self.assertFalse(result['native_queue_counts_available'])

    def test_wrong_health_or_readiness_fails(self):
        for field in ('health', 'ready'):
            with self.subTest(field=field):
                self.setUp()
                setattr(self, field, {'status': 'other'})
                with self.assertRaises(ValueError):
                    module.verify(self.get)

    def test_backlog_or_active_alert_cannot_pass_as_healthy(self):
        self.state['metrics']['processing_outstanding'] = 1
        with self.assertRaises(ValueError):
            module.verify(self.get)
        self.setUp()
        self.state['alerts'] = ['CONSUMER_HEARTBEAT_STALE']
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_stale_missing_or_error_worker_fails(self):
        for change in ({'present': False}, {'has_error': True}, {'age_seconds': 31}, {'age_seconds': float('nan')}):
            with self.subTest(change=change):
                self.setUp()
                self.state['workers'][0].update(change)
                with self.assertRaises(ValueError):
                    module.verify(self.get)

    def test_missing_rule_cannot_hide_missing_signal_implementation(self):
        del self.state['alert_rules']['CONSUMER_HEARTBEAT_MISSING']
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_historical_audit_cannot_be_silently_dropped(self):
        self.state['historical_observations'] = []
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_bad_exported_gauge_fails_even_if_json_is_healthy(self):
        self.text = render_metrics(self.state).replace(
            'trafficxx_worker_heartbeat_present{worker="consumer"} 1',
            'trafficxx_worker_heartbeat_present{worker="consumer"} 0')
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_duplicate_metric_samples_fail(self):
        self.text = render_metrics(self.state) + 'trafficxx_outbox_pending 0\n'
        with self.assertRaises(ValueError):
            module.verify(self.get)
