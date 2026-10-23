"""Live rule evidence decisions; PromQL behaviours use separate promtool fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('browse_alerts', ROOT / 'scripts/check-browse-alerts.py')
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)
spec = importlib.util.spec_from_file_location('monitoring_cases', ROOT / 'unit_tests/test_browse_monitoring.py')
monitoring_cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitoring_cases)


class BrowseAlerts(unittest.TestCase):
    def setUp(self):
        self.now = 1792771200
        self.monitor = monitoring_cases.BrowseMonitoring()
        self.monitor.setUp()
        timestamp = datetime.fromtimestamp(self.now - 2, timezone.utc).isoformat()
        self.rows = [{'name': name, 'type': 'alerting', 'duration': duration, 'query': query,
                      'state': 'inactive', 'health': 'ok', 'lastError': '', 'alerts': [],
                      'labels': {'component': 'browse-pool', 'severity': 'warning'},
                      'lastEvaluation': timestamp} for name, (duration, query) in rules.RULES.items()]

    def get(self, path):
        if path == '/api/v1/rules':
            return {'status': 'success', 'data': {'groups': [
                {'name': rules.JOB, 'interval': 15, 'rules': self.rows}]}}
        if path.startswith('/api/v1/format_query?'):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['query'][0]
            return {'status': 'success', 'data': query}
        return self.monitor.get(path)

    def test_loaded_fresh_inactive_rules_and_real_samples_are_required(self):
        result = rules.verify(self.get, clock=lambda: self.now)
        self.assertEqual(result['result'], 'BROWSE_ALERT_RULES_OK')
        self.assertEqual(result['rule_count'], 5)
        self.assertFalse(result['notification_delivery_configured'])

    def test_pending_firing_or_unhealthy_rule_cannot_pass_healthy_check(self):
        for field, value in [('state', 'pending'), ('state', 'firing'), ('health', 'err'),
                             ('lastError', 'PRIVATE_ERROR'), ('duration', 0), ('type', 'recording')]:
            self.setUp()
            self.rows[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(rules.CheckFailed):
                rules.verify(self.get, clock=lambda: self.now)

    def test_wrong_expression_missing_duplicate_or_stale_rules_fail(self):
        for fault in ('wrong-expression', 'missing', 'duplicate', 'stale'):
            self.setUp()
            if fault == 'wrong-expression':
                self.rows[0]['query'] = 'vector(0)'
            elif fault == 'missing':
                self.rows.pop()
            elif fault == 'duplicate':
                self.rows[1] = deepcopy(self.rows[0])
            else:
                self.rows[0]['lastEvaluation'] = datetime.fromtimestamp(self.now - 60, timezone.utc).isoformat()
            with self.subTest(fault=fault), self.assertRaises(rules.CheckFailed):
                rules.verify(self.get, clock=lambda: self.now)

    def test_healthy_rule_metadata_without_healthy_scrapes_is_insufficient(self):
        self.monitor.values['up'] = 0
        with self.assertRaises(ValueError):
            rules.verify(self.get, clock=lambda: self.now)

    def test_rfc3339_nanoseconds_work_on_older_python_and_timezone_is_required(self):
        expected = datetime.fromisoformat('2026-10-23T16:20:24.749338+00:00').timestamp()
        self.assertEqual(rules.evaluation_epoch('2026-10-23T16:20:24.749338300Z'), expected)
        with self.assertRaises(rules.CheckFailed):
            rules.evaluation_epoch('2026-10-23T16:20:24.749338300')
