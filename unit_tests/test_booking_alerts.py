"""Booking rule evidence must not be satisfied by Browse rules or samples."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rules = load('booking_alert_rules', 'scripts/check-browse-alerts.py')
fixtures = load('booking_alert_samples', 'unit_tests/test_booking_monitoring.py')


class BookingAlerts(unittest.TestCase):
    def setUp(self):
        self.now = 1792771200
        self.monitor = fixtures.BookingMonitoring()
        self.monitor.setUp()
        self.rows = [
            {'name': name, 'type': 'alerting', 'duration': duration, 'query': query,
             'state': 'inactive', 'health': 'ok', 'lastError': '', 'alerts': [],
             'labels': {'component': 'booking-pool', 'severity': 'warning'},
             'lastEvaluation': datetime.fromtimestamp(self.now - 2, timezone.utc).isoformat()}
            for name, (duration, query) in rules.rules_for('booking').items()]
        self.group = 'trafficxx-booking-pool'

    def get(self, path):
        if path == '/api/v1/rules':
            return {'status': 'success', 'data': {'groups': [
                {'name': self.group, 'interval': 15, 'rules': self.rows}]}}
        if path.startswith('/api/v1/format_query?'):
            expression = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['query'][0]
            return {'status': 'success', 'data': expression}
        return self.monitor.get(path)

    def verify(self):
        return rules.verify(self.get, clock=lambda: self.now, component='booking')

    def test_complete_healthy_booking_rules_require_real_booking_samples(self):
        result = self.verify()
        self.assertEqual(result['result'], 'BOOKING_ALERT_RULES_OK')
        self.assertEqual(result['target_count'], 2)
        self.assertEqual(result['rule_count'], 5)
        self.assertFalse(result['notification_delivery_configured'])
        self.monitor.booking.values['scrape_samples'] = 1
        with self.assertRaises(ValueError):
            self.verify()

    def test_browse_group_expressions_or_labels_cannot_pass_booking(self):
        for fault in ('group', 'expression', 'component', 'names'):
            self.setUp()
            if fault == 'group':
                self.group = 'trafficxx-browse-pool'
            elif fault == 'expression':
                self.rows[0]['query'] = self.rows[0]['query'].replace('booking', 'browse')
            elif fault == 'component':
                self.rows[0]['labels']['component'] = 'browse-pool'
            else:
                self.rows[0]['name'] = self.rows[0]['name'].replace('Booking', 'Browse')
            with self.subTest(fault=fault), self.assertRaises(rules.CheckFailed):
                self.verify()

    def test_active_stale_unhealthy_or_changed_holds_do_not_pass(self):
        for field, value in [('state', 'pending'), ('state', 'firing'), ('health', 'err'),
                             ('lastError', 'PRIVATE'), ('duration', 0),
                             ('lastEvaluation', '2020-01-01T00:00:00Z')]:
            self.setUp()
            self.rows[0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(rules.CheckFailed):
                self.verify()

    def test_missing_duplicate_rules_and_down_samples_fail(self):
        for fault in ('missing', 'duplicate', 'down'):
            self.setUp()
            if fault == 'missing':
                self.rows.pop()
            elif fault == 'duplicate':
                self.rows[1] = deepcopy(self.rows[0])
            else:
                self.monitor.booking.values['up'] = 0
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.verify()

    def test_unknown_component_is_rejected_before_http(self):
        with self.assertRaises(rules.CheckFailed):
            rules.verify(lambda path: self.fail('HTTP must not be called'), component='arbitrary')
