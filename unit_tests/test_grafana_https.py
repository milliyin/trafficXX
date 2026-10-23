"""Offline gateway deployment guards and real-cookie verification contracts."""
from contextlib import contextmanager
import copy
import base64
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location(
    'grafana_https', Path(__file__).resolve().parents[1] / 'scripts/grafana-https.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
HOST = 'trafficxx-grafana-e8708a85.g8ddfjd0h4ewaaf4.eastus.sysgen.cloudapp.azure.com'
ENDPOINT = {'hostname': HOST, 'ip_address': '20.124.118.213'}
VALUES = {'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': 'A' * 64}


class Browser:
    hostname = HOST

    def __init__(self):
        self.logged = False
        self.cookie = True
        self.redirect = '/'
        self.login_calls = 0
        self.logout_calls = 0
        self.bad_dashboard = False

    def secure_session(self):
        return self.cookie

    def raw(self, method, path, payload=None, scheme='https'):
        if scheme == 'http':
            return 308, {'Location': 'https://' + HOST + '/'}, b''
        if method == 'POST' and path == '/login':
            self.login_calls += 1
            self.logged = True
            return 200, {}, json.dumps({'redirectUrl': self.redirect}).encode()
        if path == '/logout':
            self.logout_calls += 1
            self.logged = False
            return 302, {'Location': '/login'}, b''
        if path in ['/metrics', '/debug/pprof/']:
            return 404, {}, b''
        if path in ['/api/user', '/api/dashboards/uid/trafficxx-operations']:
            return (200 if self.logged else 401), {}, b''
        raise AssertionError('Unexpected raw request')

    def get(self, path):
        if path == '/api/health':
            return {'database': 'ok', 'version': '13.2.4'}
        if not self.logged:
            raise AssertionError('Protected read without cookie login')
        if path == '/api/user':
            return {'id': 7, 'login': 'admin', 'isGrafanaAdmin': True}
        if path == '/api/datasources/uid/trafficxx-prometheus':
            return {'uid': 'trafficxx-prometheus', 'type': 'prometheus',
                    'url': 'http://prometheus.trafficxx-monitoring.svc.cluster.local:9090'}
        if path.endswith('/health'):
            return {'status': 'OK'}
        if path == '/api/dashboards/uid/trafficxx-operations':
            if self.bad_dashboard: raise ValueError('Fake bad dashboard')
            return {'meta': {'provisioned': True}, 'dashboard': {
                'uid': 'trafficxx-operations', 'title': 'trafficXX | Operations',
                'panels': [{'datasource': {'uid': 'trafficxx-prometheus'}} for _ in range(12)]}}
        if '/api/v1/query?' in path:
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'value': [1, '1']}]}}
        raise AssertionError('Unexpected JSON request')


class Gateway(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.existing = False
        self.fail_gateway = False
        self.bad_ip = False

    def call(self, args, payload=None):
        self.calls.append((args, payload))
        if 'storageclass' in args: return 'disk.csi.azure.com'
        if 'service/grafana-https' in args:
            if args[-1] == 'json':
                return json.dumps({'status': {'loadBalancer': {'ingress': [
                    {'ip': '20.1.1.1' if self.bad_ip else ENDPOINT['ip_address']} ]}}})
            return 'service/grafana-https' if self.existing else ''
        if args[1:4] == ['rollout', 'status', 'deployment/grafana-https'] and self.fail_gateway:
            raise RuntimeError('Fake invalid gateway config')
        if args[1:3] == ['get', 'deployment/grafana-https']:
            return 'deployment/grafana-https' if self.existing else ''
        return ''

    @contextmanager
    def connect(self):
        def request(method, path, password, payload=None):
            self.assertEqual(password, VALUES['GF_SECURITY_ADMIN_PASSWORD'])
            return [] if path.endswith('/auth-tokens') else {'id': 7, 'login': 'admin', 'isGrafanaAdmin': True}
        yield request

    def deploy(self):
        with patch.object(module, 'private_credentials', return_value=VALUES):
            return module.deploy(self.call, ENDPOINT, lambda stage: None, self.connect, lambda seconds: None)

    def test_publish_follows_rotated_credential_gate_and_successful_gateway_rollout(self):
        result = self.deploy()
        self.assertFalse(result['tls_verified'])
        applied = [args for args, _ in self.calls if args[1] == 'apply']
        self.assertTrue(applied[-1][-1].endswith('grafana-https-service.yaml'))
        rollout = next(i for i, (args, _) in enumerate(self.calls)
                       if args[1:4] == ['rollout', 'status', 'deployment/grafana-https'])
        publish = next(i for i, (args, _) in enumerate(self.calls)
                       if args[-1].endswith('grafana-https-service.yaml'))
        self.assertLess(rollout, publish)
        self.assertNotIn(VALUES['GF_SECURITY_ADMIN_PASSWORD'], json.dumps(self.calls))
        env = next(args for args, _ in self.calls if args[1:3] == ['set', 'env'])
        self.assertIn('GF_SECURITY_COOKIE_SAMESITE=lax', env)
        self.assertIn('GF_AUTH_ANONYMOUS_ENABLED=false', env)

    def test_failed_credential_gate_never_mutates_resources(self):
        with patch.object(module, 'private_credentials', side_effect=ValueError('Unrotated')):
            with self.assertRaises(ValueError):
                module.deploy(self.call, ENDPOINT, lambda stage: None, self.connect)
        self.assertFalse(any(args[1] in ('apply', 'set') for args, _ in self.calls))

    def test_failed_gateway_rollout_does_not_publish_service(self):
        self.fail_gateway = True
        with self.assertRaises(RuntimeError): self.deploy()
        self.assertFalse(any(args[-1].endswith('grafana-https-service.yaml') for args, _ in self.calls))

    def test_wrong_load_balancer_address_is_rejected(self):
        self.bad_ip = True
        with self.assertRaises(ValueError): self.deploy()

    def test_existing_gateway_restarts_to_reload_config(self):
        self.existing = True
        self.deploy()
        self.assertTrue(any(args[1:4] == ['rollout', 'restart', 'deployment/grafana-https']
                            for args, _ in self.calls))

    def test_endpoint_rejects_unexpected_host_resource_region_and_private_ip(self):
        source = {'name': 'trafficxx-lab-grafana-ip', 'resourceGroup': 'trafficxx-lab-rg',
                  'location': 'eastus', 'dnsSettings': {'fqdn': HOST}, 'ipAddress': ENDPOINT['ip_address']}
        self.assertEqual(module.endpoint_metadata(source), ENDPOINT)
        for key, value in [('location', 'westus'), ('name', 'other'), ('ipAddress', '10.0.0.1'),
                           ('dnsSettings', {'fqdn': 'attacker.example'})]:
            changed = copy.deepcopy(source)
            changed[key] = value
            with self.assertRaises(ValueError): module.endpoint_metadata(changed)

    def test_initial_gate_rejects_active_sessions(self):
        def request(method, path, password):
            return [{'id': 4}] if path.endswith('/auth-tokens') else {'id': 7, 'login': 'admin', 'isGrafanaAdmin': True}
        with self.assertRaises(ValueError): module.credential_gate(request, VALUES, initial=True)
        module.credential_gate(request, VALUES, initial=False)

    def test_rotated_secret_restores_lost_private_file_without_secret_output(self):
        secret = {'metadata': {'annotations': {'trafficxx/password-rotated-at': '2026-10-23T10:00:00+00:00'}},
                  'data': {key: base64.b64encode(value.encode()).decode() for key, value in VALUES.items()}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.grafana'
            result = module.private_credentials(lambda *args: json.dumps(secret), path)
            self.assertEqual(result, VALUES)
            self.assertEqual(module.rotation.credentials.read_file(path), VALUES)

    def test_unrotated_secret_or_pending_rotation_stops_before_restoring_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.grafana'
            with self.assertRaises(ValueError):
                module.private_credentials(lambda *args: json.dumps({'metadata': {}}), path)
            self.assertFalse(path.exists())
            path.with_name('.env.grafana.next').write_text('pending')
            with self.assertRaises(ValueError): module.private_credentials(None, path)

    def test_rollout_gets_long_timeout_while_secret_output_remains_captured(self):
        with patch.object(module.subprocess, 'run', return_value=Mock(returncode=0, stdout='')) as run:
            module.command(['kubectl', 'rollout', 'status', 'deployment/grafana-https'])
            self.assertEqual(run.call_args.kwargs['timeout'], 660)
            self.assertIsNone(run.call_args.kwargs['stdout'])
            module.command(['kubectl', 'get', 'secret/grafana-admin'])
            self.assertEqual(run.call_args.kwargs['stdout'], module.subprocess.PIPE)


class HttpsVerification(unittest.TestCase):
    def resolve(self, *args):
        return [(None, None, None, None, (ENDPOINT['ip_address'], 443))]

    def test_healthy_public_preflight_and_cookie_authenticated_dashboard(self):
        client = Browser()
        module.public_preflight(client, ENDPOINT, self.resolve)
        result = module.verify_login(client, VALUES)
        self.assertEqual(result['panel_count'], 12)
        self.assertEqual(client.login_calls, 1)
        self.assertEqual(client.logout_calls, 1)
        self.assertFalse(client.logged)

    def test_missing_cookie_or_wrong_redirect_still_cleans_diagnostic_session(self):
        for broken in ('cookie', 'redirect', 'dashboard'):
            client = Browser()
            if broken == 'cookie': client.cookie = False
            if broken == 'redirect': client.redirect = 'https://attacker.example/'
            if broken == 'dashboard': client.bad_dashboard = True
            with self.assertRaises(ValueError): module.verify_login(client, VALUES)
            self.assertEqual(client.logout_calls, 1)
            self.assertFalse(client.logged)

    def test_wrong_dns_is_rejected_before_login(self):
        client = Browser()
        with self.assertRaises(ValueError):
            module.public_preflight(client, ENDPOINT, lambda *args: [])
        self.assertEqual(client.login_calls, 0)

    def test_readiness_retry_does_not_repeat_credential_submissions(self):
        client = Browser()
        with patch.object(module, 'private_credentials', return_value=VALUES), patch.object(
                module, 'public_preflight', side_effect=[ValueError('Not ready'), None]):
            result = module.check(None, ENDPOINT, lambda stage: None,
                                  sleep=lambda seconds: None, attempts=2, client=client)
        self.assertEqual(result['result'], 'GRAFANA_HTTPS_OK')
        self.assertEqual(client.login_calls, 1)

    def test_bounded_readiness_failure_never_submits_credentials(self):
        client = Browser()
        with patch.object(module, 'private_credentials', return_value=VALUES), patch.object(
                module, 'public_preflight', side_effect=ValueError('Not ready')):
            with self.assertRaises(ValueError):
                module.check(None, ENDPOINT, lambda stage: None,
                             sleep=lambda seconds: None, attempts=2, client=client)
        self.assertEqual(client.login_calls, 0)

    def test_plain_http_cannot_be_used_for_credential_submission(self):
        client = module.PublicClient(HOST)
        with self.assertRaises(ValueError):
            client.raw('POST', '/login', VALUES, scheme='http')
