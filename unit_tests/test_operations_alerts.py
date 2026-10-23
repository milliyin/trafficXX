"""Live-drill decisions and guaranteed helper restoration using fake cloud calls."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock
from test_consumer_backlog import Cloud, module as backlog
from test_monitoring import VALUES, ROWS, EXPECTED
from test_operations_http import module as http
from trafficxx.monitoring import evaluate, render_metrics

spec = importlib.util.spec_from_file_location(
    'operations_alerts', Path(__file__).resolve().parents[1] / 'scripts/check-operations-alerts.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def active():
    rows = deepcopy(ROWS)
    next(w for w in rows if w['name'] == 'consumer')['age_seconds'] = 45
    return evaluate({**VALUES, 'processing_outstanding': 2, 'processing_oldest_seconds': 40}, rows, 30, EXPECTED)


def getter(state, metric_text=None):
    def get(path, text=False):
        return {'/healthz': {'status': 'alive'}, '/readyz': {'status': 'ready'},
                '/backlog': state, '/metrics': metric_text if metric_text is not None else render_metrics(state)}[path]
    return get


class OperationsAlerts(unittest.TestCase):
    def test_both_json_and_exported_signals_are_required(self):
        state = active()
        result = module.active_signals(getter(state))
        self.assertTrue(result['active_alert_metrics_verified'])
        self.assertEqual(set(result['alerts']), {'PROCESSING_BACKLOG_AGING', 'CONSUMER_HEARTBEAT_STALE'})

    def test_json_alerts_without_exported_gauges_cannot_pass(self):
        state = active()
        metrics = render_metrics(state).replace('alert="CONSUMER_HEARTBEAT_STALE"} 1',
                                                'alert="CONSUMER_HEARTBEAT_STALE"} 0')
        self.assertIsNone(module.active_signals(getter(state, metrics)))

    def test_historical_dead_letter_observation_does_not_prove_current_alerts(self):
        healthy = evaluate(VALUES, ROWS, 30, EXPECTED)
        self.assertIsNone(module.active_signals(getter(healthy)))

    def test_wait_is_bounded_without_alert_evidence(self):
        sleep = MagicMock()
        with self.assertRaises(TimeoutError):
            module.wait_for_alerts(getter(evaluate(VALUES, ROWS, 30, EXPECTED)), sleep)
        self.assertEqual(sleep.call_count, 18)

    def scenario(self, mode='success'):
        cloud = Cloud()
        healthy = evaluate(VALUES, ROWS, 30, EXPECTED)

        def get(path, text=False):
            state = active() if cloud.replicas == 0 else healthy
            if mode == 'no-alerts':
                state = healthy
            if mode == 'no-clear' and cloud.created:
                state = active()
            return getter(state)(path, text)

        def drill(**kwargs):
            return backlog.run(cloud, lambda _: None, **kwargs)
        return cloud, get, drill

    def test_complete_drill_clears_http_gauges_and_checks_native_sql_recovery(self):
        cloud, get, drill = self.scenario()
        result = module.run(get, drill, http.verify, lambda _: None)
        self.assertEqual(result['result'], 'OPERATIONS_ALERT_RECOVERY_OK')
        self.assertEqual(cloud.replicas, 1)
        self.assertEqual(result['after']['alerts'], [])
        self.assertEqual(result['backlog_recovery']['queue_after']['total'], 0)

    def test_monitoring_timeout_restores_consumer(self):
        cloud, get, drill = self.scenario('no-alerts')
        with self.assertRaises(TimeoutError):
            module.run(get, drill, http.verify, lambda _: None)
        self.assertEqual(cloud.replicas, 1)

    def test_failed_clearing_is_not_recovery_success_and_consumer_is_restored(self):
        cloud, get, drill = self.scenario('no-clear')
        with self.assertRaises(TimeoutError):
            module.run(get, drill, http.verify, lambda _: None)
        self.assertEqual(cloud.replicas, 1)

    def test_bad_baseline_never_pauses_or_creates_work(self):
        drill = MagicMock()
        with self.assertRaises(ValueError):
            module.run(getter(active()), drill, http.verify, lambda _: None)
        drill.assert_not_called()

    def test_observation_window_retains_pause_then_restores_and_verifies_recovery(self):
        cloud, get, drill = self.scenario()
        sleeps = []

        def observe(seconds):
            self.assertEqual(cloud.replicas, 0)
            sleeps.append(seconds)
        result = module.run(get, drill, http.verify, observe, observe_seconds=45)
        self.assertEqual(sum(sleeps), 45)
        self.assertEqual(result['result'], 'OPERATIONS_ALERT_RECOVERY_OK')
        self.assertEqual(cloud.replicas, 1)

    def test_observation_failure_still_restores_consumer(self):
        cloud, get, drill = self.scenario()

        def fail(seconds):
            raise OSError('Fake observer failure')
        with self.assertRaises(OSError):
            module.run(get, drill, http.verify, fail, observe_seconds=45)
        self.assertEqual(cloud.replicas, 1)

    def test_invalid_observation_window_cannot_pause_consumer(self):
        for seconds in [-1, 121, True, 1.5]:
            drill = MagicMock()
            with self.assertRaises(ValueError):
                module.run(None, drill, http.verify, observe_seconds=seconds)
            drill.assert_not_called()
