"""Grafana data source/provisioning decisions with fake API responses."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'grafana_dashboard', Path(__file__).resolve().parents[1] / 'scripts/check-grafana-dashboard.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GrafanaDashboard(unittest.TestCase):
    def setUp(self):
        self.health = {'database': 'ok', 'version': '13.2.4'}
        self.source = {'uid': module.SOURCE, 'type': 'prometheus',
                       'url': 'http://prometheus.trafficxx-monitoring.svc.cluster.local:9090'}
        self.source_health = {'status': 'OK'}
        self.dashboard = {'meta': {'provisioned': True}, 'dashboard': {
            'uid': module.UID, 'title': 'trafficXX | Operations',
            'panels': [{'datasource': {'uid': module.SOURCE}} for _ in range(12)]}}
        self.query = {'status': 'success', 'data': {'resultType': 'vector',
                      'result': [{'value': [123, '1']}]}}

    def get(self, path):
        if path == '/api/health':
            return self.health
        if path.endswith('/health'):
            return self.source_health
        if path.startswith('/api/datasources/proxy/'):
            return self.query
        if path.startswith('/api/datasources/uid/'):
            return self.source
        return self.dashboard

    def test_provisioned_dashboard_and_actual_data_source_query_pass(self):
        result = module.verify(self.get)
        self.assertEqual(result['result'], 'GRAFANA_DASHBOARD_OK')
        self.assertEqual(result['panel_count'], 12)
        self.assertFalse(result['notification_delivery_configured'])

    def test_unhealthy_database_or_data_source_fails(self):
        for field in ('health', 'source_health'):
            with self.subTest(field=field):
                self.setUp()
                setattr(self, field, {'status': 'bad', 'database': 'bad'})
                with self.assertRaises(ValueError):
                    module.verify(self.get)

    def test_wrong_data_source_url_is_rejected(self):
        self.source['url'] = 'http://other'
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_wrong_or_unprovisioned_dashboard_fails(self):
        for field in ('uid', 'title', 'panels'):
            with self.subTest(field=field):
                self.setUp()
                self.dashboard['dashboard'][field] = [] if field == 'panels' else 'other'
                with self.assertRaises(ValueError):
                    module.verify(self.get)
        self.setUp()
        self.dashboard['meta']['provisioned'] = False
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_wrong_panel_data_source_fails(self):
        self.dashboard['dashboard']['panels'][0]['datasource']['uid'] = 'other'
        with self.assertRaises(ValueError):
            module.verify(self.get)

    def test_empty_down_or_failed_actual_query_fails(self):
        variants = [dict(status='error'), {'status': 'success', 'data': {'resultType': 'vector', 'result': []}},
                    {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'value': [123, '0']}]}}]
        for query in variants:
            with self.subTest(query=query):
                self.query = deepcopy(query)
                with self.assertRaises(ValueError):
                    module.verify(self.get)
