"""Offline exposition and stored-sample evidence checks."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metrics = load('browse_metrics', 'trafficxx/browse_metrics.py')
prom = load('browse_monitoring', 'scripts/check-browse-monitoring.py')
grafana = load('pool_grafana', 'scripts/check-grafana-dashboard.py')


class BrowseMonitoring(unittest.TestCase):
    def setUp(self):
        self.instances = ['10.244.2.180:8000', '10.244.3.157:8000']
        self.targets = [{'labels': {'job': prom.JOB, 'instance': instance}, 'health': 'up',
                         'lastError': '', 'scrapeUrl': 'http://' + instance + '/metrics'}
                        for instance in self.instances]
        self.values = dict(up=1, freshness_seconds=2, scrape_samples=3, size=3, limit=4,
                           available=3, waiting=0, borrows_total=40, borrow_errors_total=0,
                           connection_attempts_total=3, connection_errors_total=0,
                           connections_lost_total=0, bad_returns_total=0)
        self.dashboard = {'meta': {'provisioned': True}, 'dashboard': {
            'uid': grafana.POOL_UID, 'title': 'trafficXX | Browse database pool',
            'panels': [{'datasource': {'uid': grafana.SOURCE}} for _ in range(8)]}}

    def get(self, path):
        if path == '/api/v1/targets':
            return {'data': {'activeTargets': self.targets}}
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)['query'][0]
        key = next(name for name, expression in prom.QUERIES.items() if expression == query)
        return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
            {'metric': {'instance': instance}, 'value': [123, str(self.values[key])]}
            for instance in self.instances]}}

    def grafana_get(self, path):
        if path == '/api/health':
            return {'database': 'ok', 'version': '13.2.4'}
        if path == '/api/datasources/uid/' + grafana.SOURCE:
            return {'uid': grafana.SOURCE, 'type': 'prometheus',
                    'url': 'http://prometheus.trafficxx-monitoring.svc.cluster.local:9090'}
        if path.endswith('/health'):
            return {'status': 'OK'}
        if path == '/api/dashboards/uid/' + grafana.UID:
            return {'meta': {'provisioned': True}, 'dashboard': {
                'uid': grafana.UID, 'title': 'trafficXX | Operations',
                'panels': [{'datasource': {'uid': grafana.SOURCE}} for _ in range(12)]}}
        if path == '/api/dashboards/uid/' + grafana.POOL_UID:
            return self.dashboard
        prefix = '/api/datasources/proxy/uid/' + grafana.SOURCE
        suffix = path[len(prefix):]
        if 'trafficxx-operations' in urllib.parse.unquote(suffix):
            return {'status': 'success', 'data': {'resultType': 'vector',
                    'result': [{'metric': {}, 'value': [123, '1']}]}}
        return self.get(suffix)

    def test_exposition_has_stable_types_and_no_secret_fields_or_labels(self):
        result = metrics.render({'pool_size': 3, 'pool_max': 4, 'requests_num': 32,
                                 'password': 'PRIVATE_TEST_SECRET', 'instance_id': 'PRIVATE_ID'})
        self.assertIn('# TYPE trafficxx_browse_pool_size gauge', result)
        self.assertIn('# TYPE trafficxx_browse_pool_borrows_total counter', result)
        self.assertIn('trafficxx_browse_pool_borrows_total 32', result)
        self.assertIn('trafficxx_browse_pool_connection_errors_total 0', result)
        self.assertNotIn('PRIVATE', result)
        self.assertNotIn('{', result)

    def test_invalid_statistics_cannot_be_exposed(self):
        for value in (-1, 'SECRET', True, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                metrics.render({'pool_size': value})

    def test_two_fresh_targets_and_correlated_stored_samples_pass(self):
        result = prom.verify(self.get, self.instances)
        self.assertEqual(result['result'], 'BROWSE_PROMETHEUS_OK')
        self.assertEqual(len(result['per_instance']), 2)
        self.assertFalse(result['notification_delivery_configured'])

    def test_wrong_pods_or_target_health_prevent_pass(self):
        with self.assertRaises(ValueError):
            prom.verify(self.get, ['10.244.0.1:8000', '10.244.0.2:8000'])
        for field, value in [('health', 'down'), ('lastError', 'DO_NOT_LOG_RAW'),
                             ('scrapeUrl', 'http://external.example/metrics')]:
            self.setUp()
            self.targets[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                prom.verify(self.get)

    def test_single_duplicate_or_unrelated_targets_prevent_pass(self):
        for mode in ('single', 'duplicate', 'wrong-network'):
            self.setUp()
            if mode == 'single':
                self.targets.pop()
            elif mode == 'duplicate':
                self.targets[1] = copy.deepcopy(self.targets[0])
            else:
                self.targets[0]['labels']['instance'] = '192.168.1.1:8000'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                prom.verify(self.get)

    def test_stale_single_sample_or_unhealthy_pool_cannot_pass(self):
        for key, value in [('freshness_seconds', 46), ('scrape_samples', 1), ('size', 5),
                           ('available', 4), ('waiting', 1), ('limit', 8), ('up', 0),
                           ('borrows_total', 0), ('connection_attempts_total', 0)]:
            self.setUp()
            self.values[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                prom.verify(self.get)

    def test_missing_metric_instance_nonfinite_and_duplicate_samples_fail(self):
        for rows in ([{'metric': {'instance': 'x'}, 'value': [0, 'NaN']}],
                     [{'metric': {'instance': 'x'}, 'value': [0, '1']}] * 2):
            with self.assertRaises(ValueError):
                prom.vector({'status': 'success', 'data': {'resultType': 'vector', 'result': rows}})
        self.instances = self.instances[:1]
        with self.assertRaises(ValueError):
            prom.verify(self.get)

    def test_grafana_requires_both_dashboards_and_live_datasource_samples(self):
        result = grafana.verify_pool(self.grafana_get)
        self.assertEqual(result['result'], 'GRAFANA_BROWSE_POOL_OK')
        self.assertEqual(result['panel_count'], 8)
        self.assertTrue(result['operations_dashboard_verified'])
        self.values['up'] = 0
        with self.assertRaises(ValueError):
            grafana.verify_pool(self.grafana_get)

    def test_unprovisioned_wrong_panel_or_datasource_dashboard_fails(self):
        for mode in ('unprovisioned', 'missing-panel', 'wrong-source'):
            self.setUp()
            if mode == 'unprovisioned':
                self.dashboard['meta']['provisioned'] = False
            elif mode == 'missing-panel':
                self.dashboard['dashboard']['panels'].pop()
            else:
                self.dashboard['dashboard']['panels'][0]['datasource']['uid'] = 'wrong'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                grafana.verify_pool(self.grafana_get)

    def test_dashboard_queries_use_pool_job_and_provisioned_datasource(self):
        document = json.loads((ROOT / 'monitoring/grafana/trafficxx-browse-pool.json').read_text())
        self.assertEqual(document['uid'], grafana.POOL_UID)
        self.assertEqual(len(document['panels']), 8)
        self.assertEqual(len({panel['id'] for panel in document['panels']}), 8)
        for panel in document['panels']:
            self.assertEqual(panel['datasource']['uid'], grafana.SOURCE)
            for target in panel['targets']:
                self.assertIn(prom.FILTER, target['expr'])
