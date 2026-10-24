"""Booking observations must use their own job and preserve existing dashboards."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('booking_monitor_fixtures', 'unit_tests/test_browse_monitoring.py')
prom = load('booking_pool_monitor', 'scripts/check-browse-monitoring.py')
grafana = load('booking_pool_grafana', 'scripts/check-grafana-dashboard.py')
metrics = load('booking_pool_metrics', 'trafficxx/pool_metrics.py')


class BookingMonitoring(unittest.TestCase):
    def setUp(self):
        self.browse = fixtures.BrowseMonitoring()
        self.browse.setUp()
        self.booking = fixtures.BrowseMonitoring()
        self.booking.setUp()
        self.booking.instances = ['10.244.3.218:8000', '10.244.2.95:8000']
        self.booking.targets = [
            {'labels': {'job': 'trafficxx-booking-pool', 'instance': instance}, 'health': 'up',
             'lastError': '', 'scrapeUrl': 'http://' + instance + '/metrics'}
            for instance in self.booking.instances]
        document = json.loads((ROOT / 'monitoring/grafana/trafficxx-booking-pool.json').read_text())
        self.dashboard = {'meta': {'provisioned': True}, 'dashboard': document}

    def get(self, path):
        # Existing fixture generates numeric series, while Booking's real
        # verifier must query its own job/prefix to reach those responses.
        return self.booking.get(path.replace('booking', 'browse'))

    def grafana_get(self, path):
        if path == '/api/dashboards/uid/' + grafana.BOOKING_POOL_UID:
            return self.dashboard
        prefix = '/api/datasources/proxy/uid/' + grafana.SOURCE
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            if 'booking' in suffix:
                return self.get(suffix)
            if suffix == '/api/v1/targets':
                payload = deepcopy(self.browse.get(suffix))
                payload['data']['activeTargets'] += self.booking.targets
                return payload
        return self.browse.grafana_get(path)

    def test_metrics_are_booking_specific_and_ignore_credentials(self):
        text = metrics.render({'pool_size': 2, 'password': 'PRIVATE', 'conninfo': 'PRIVATE'}, component='booking')
        self.assertIn('# TYPE trafficxx_booking_pool_size gauge', text)
        self.assertIn('trafficxx_booking_pool_size 2', text)
        self.assertIn('trafficxx_booking_pool_borrows_total 0', text)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('browse', text)
        self.assertNotIn('{', text)
        with self.assertRaises(ValueError):
            metrics.render({}, component='arbitrary-label-injection')

    def test_exact_booking_targets_and_stored_samples_are_required(self):
        result = prom.verify(self.get, self.booking.instances, component='booking')
        self.assertEqual(result['result'], 'BOOKING_PROMETHEUS_OK')
        self.assertEqual(result['target_count'], 2)
        with self.assertRaises(ValueError):
            prom.verify(self.get, component='browse')
        with self.assertRaises(ValueError):
            prom.verify(self.get, self.browse.instances, component='booking')

    def test_stale_missing_or_oversized_booking_samples_fail(self):
        for key, value in [('freshness_seconds', 46), ('scrape_samples', 1), ('size', 5)]:
            self.setUp()
            self.booking.values[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                prom.verify(self.get, component='booking')

    def test_booking_dashboard_checks_all_three_dashboards_and_live_samples(self):
        result = grafana.verify_booking_pool(self.grafana_get)
        self.assertEqual(result['result'], 'GRAFANA_BOOKING_POOL_OK')
        self.assertEqual(result['panel_count'], 8)
        self.assertTrue(result['browse_dashboard_and_rules_verified'])
        self.browse.rules.clear()
        with self.assertRaises(ValueError):
            grafana.verify_booking_pool(self.grafana_get)

    def test_wrong_dashboard_source_or_unhealthy_booking_cannot_pass(self):
        for fault in ('unprovisioned', 'wrong-source', 'browse-query', 'down'):
            self.setUp()
            if fault == 'unprovisioned':
                self.dashboard['meta']['provisioned'] = False
            elif fault == 'wrong-source':
                self.dashboard['dashboard']['panels'][0]['datasource']['uid'] = 'wrong'
            elif fault == 'browse-query':
                self.dashboard['dashboard']['panels'][0]['targets'][0]['expr'] = 'up{job="trafficxx-browse-pool"}'
            else:
                self.booking.values['up'] = 0
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                grafana.verify_booking_pool(self.grafana_get)
