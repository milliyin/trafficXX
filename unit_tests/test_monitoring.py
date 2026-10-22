"""Monitoring behavior under missing workers, lag, errors and recovery."""
from copy import deepcopy
import unittest
from trafficxx.monitoring import evaluate, expected_workers, render_metrics


VALUES = dict(outbox_pending=0, outbox_oldest_seconds=0, processing_outstanding=0,
              processing_oldest_seconds=0, publish_failures_total=1, processed_total=16,
              processing_failures_total=0, observed_dead_letters_total=1)
ROWS = [dict(name=name, last_seen_at='timestamp', last_error=None, age_seconds=1)
        for name in ('publisher', 'consumer')]
EXPECTED = ('publisher', 'consumer')


class Monitoring(unittest.TestCase):
    def test_healthy_pipeline_with_history_has_no_active_alerts(self):
        state = evaluate(VALUES, ROWS, 30, EXPECTED)
        self.assertEqual(state['alerts'], [])
        self.assertEqual(state['historical_observations'], ['DEAD_LETTERS_PREVIOUSLY_OBSERVED'])
        self.assertFalse(state['native_queue_counts_available'])
        self.assertEqual(state['metrics']['publish_failures_total'], 1)

    def test_missing_expected_worker_alerts_even_without_backlog(self):
        state = evaluate(VALUES, ROWS[:1], 30, EXPECTED)
        self.assertEqual(state['alerts'], ['CONSUMER_HEARTBEAT_MISSING'])
        missing = next(w for w in state['workers'] if w['name'] == 'consumer')
        self.assertFalse(missing['present'])
        self.assertIsNone(missing['age_seconds'])

    def test_stale_and_error_workers_are_detected_and_raw_error_redacted(self):
        rows = deepcopy(ROWS)
        rows[0].update(age_seconds=30, last_error='DO_NOT_EXPORT_RAW')
        state = evaluate(VALUES, rows, 30, EXPECTED)
        self.assertEqual(state['alerts'], ['PUBLISHER_ERROR', 'PUBLISHER_HEARTBEAT_STALE'])
        self.assertNotIn('DO_NOT_EXPORT_RAW', str(state))
        self.assertNotIn('DO_NOT_EXPORT_RAW', render_metrics(state))

    def test_only_aged_nonempty_backlogs_alert_and_recovery_clears(self):
        values = {**VALUES, 'outbox_pending': 2, 'outbox_oldest_seconds': 30,
                  'processing_outstanding': 1, 'processing_oldest_seconds': 31}
        self.assertEqual(evaluate(values, ROWS, 30, EXPECTED)['alerts'],
                         ['OUTBOX_BACKLOG_AGING', 'PROCESSING_BACKLOG_AGING'])
        self.assertEqual(evaluate({**values, 'outbox_pending': 0, 'processing_outstanding': 0},
                                  ROWS, 30, EXPECTED)['alerts'], [])
        self.assertEqual(evaluate({**values, 'outbox_oldest_seconds': 29, 'processing_oldest_seconds': 29},
                                  ROWS, 30, EXPECTED)['alerts'], [])

    def test_metrics_export_worker_presence_age_errors_and_zero_one_alert_states(self):
        state = evaluate(VALUES, ROWS[:1], 30, EXPECTED)
        text = render_metrics(state)
        self.assertIn('trafficxx_worker_heartbeat_present{worker="consumer"} 0', text)
        self.assertNotIn('trafficxx_worker_heartbeat_age_seconds{worker="consumer"}', text)
        self.assertIn('trafficxx_worker_heartbeat_age_seconds{worker="publisher"} 1.0', text)
        self.assertIn('trafficxx_alert_active{alert="CONSUMER_HEARTBEAT_MISSING"} 1', text)
        self.assertIn('trafficxx_alert_active{alert="OUTBOX_BACKLOG_AGING"} 0', text)
        self.assertIn('# TYPE trafficxx_observed_dead_letters_total counter', text)
        self.assertNotIn('native_queue_depth', text)
        self.assertTrue(text.endswith('\n'))

    def test_local_empty_expected_list_preserves_backlog_only_mode(self):
        self.assertEqual(evaluate({**VALUES, 'observed_dead_letters_total': 0}, [], 30)['alerts'], [])
        self.assertEqual(expected_workers(''), [])

    def test_expected_worker_config_is_trimmed_deduplicated_and_validated(self):
        self.assertEqual(expected_workers(' publisher, consumer,publisher '), ['consumer', 'publisher'])
        for value in ('worker"bad', 'publisher\nother', 'UPPER', 'x' * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                expected_workers(value)

    def test_invalid_thresholds_and_recorded_ages_cannot_hide_failure(self):
        for threshold in (0, -1, 'nan', 'inf', 'wrong'):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                evaluate(VALUES, ROWS, threshold, EXPECTED)
        for age in (-1, float('nan'), float('inf')):
            with self.subTest(age=age), self.assertRaises(ValueError):
                evaluate(VALUES, [{**ROWS[0], 'age_seconds': age}], 30, EXPECTED)

    def test_reappearing_fresh_worker_clears_missing_alert(self):
        self.assertIn('CONSUMER_HEARTBEAT_MISSING', evaluate(VALUES, ROWS[:1], 30, EXPECTED)['alerts'])
        self.assertNotIn('CONSUMER_HEARTBEAT_MISSING', evaluate(VALUES, ROWS, 30, EXPECTED)['alerts'])

    def test_label_like_recorded_names_cannot_inject_metric_samples(self):
        with self.assertRaises(ValueError):
            evaluate(VALUES, [{**ROWS[0], 'name': 'x"}\ninjected 1'}], 30, EXPECTED)
