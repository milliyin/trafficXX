"""User-run Cloud Shell deployment/check for Grafana's authenticated HTTPS route."""
from datetime import datetime
import http.cookiejar
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
NS = 'trafficxx-monitoring'
spec = importlib.util.spec_from_file_location('grafana_rotation', ROOT / 'scripts/rotate-grafana-password.py')
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)
spec = importlib.util.spec_from_file_location('grafana_dashboard', ROOT / 'scripts/check-grafana-dashboard.py')
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


def command(args, payload=None):
    rollout = args[:3] == ['kubectl', 'rollout', 'status']
    result = subprocess.run(args, input=payload, text=True, stderr=subprocess.PIPE,
                            stdout=None if rollout else subprocess.PIPE,
                            timeout=660 if rollout else 90)
    if result.returncode:
        raise RuntimeError('Gateway command failed')
    return (result.stdout or '').strip()


def endpoint_metadata(value):
    hostname = value['dnsSettings']['fqdn']
    if (value['name'] != 'trafficxx-lab-grafana-ip'
            or value['resourceGroup'].lower() != 'trafficxx-lab-rg'
            or value['location'] != 'eastus'
            or not re.fullmatch(r'trafficxx-grafana-e8708a85(?:\.[a-z0-9-]+)?\.eastus\.(?:sysgen\.)?cloudapp\.azure\.com', hostname)
            or not ipaddress.IPv4Address(value['ipAddress']).is_global):
        raise ValueError('Unexpected reserved Grafana address')
    return {'hostname': hostname, 'ip_address': value['ipAddress']}


def preflight(call):
    if call(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85':
        raise ValueError('Select the lab sponsorship subscription')
    if call(['kubectl', 'config', 'current-context']) != 'trafficxx-lab-aks':
        raise ValueError('Select trafficxx-lab-aks context')
    return endpoint_metadata(json.loads(call([
        'az', 'network', 'public-ip', 'show', '-g', 'trafficxx-lab-rg',
        '-n', 'trafficxx-lab-grafana-ip', '-o', 'json'])))


def private_credentials(call, path):
    if path.with_name(path.name + '.next').exists():
        raise ValueError('Complete pending credential rotation before publishing')
    secret = json.loads(call(['kubectl', 'get', 'secret/grafana-admin', '-n', NS, '-o', 'json']))
    rotated = secret['metadata'].get('annotations', {}).get('trafficxx/password-rotated-at')
    if not rotated or datetime.fromisoformat(rotated).tzinfo is None:
        raise ValueError('Verified credential rotation marker is required')
    rotation.credentials.ensure(call, path)
    return rotation.credentials.read_file(path)


def credential_gate(request, values, initial):
    password = values['GF_SECURITY_ADMIN_PASSWORD']
    user_id = rotation.admin_identity(request('GET', '/api/user', password))
    tokens = request('GET', f'/api/admin/users/{user_id}/auth-tokens', password)
    if not isinstance(tokens, list) or (initial and tokens):
        raise ValueError('Initial public release requires verified session revocation')


def wait_address(call, expected_ip, sleep=time.sleep, attempts=60):
    for attempt in range(attempts):
        service = json.loads(call(['kubectl', 'get', 'service/grafana-https', '-n', NS, '-o', 'json']))
        ingress = service.get('status', {}).get('loadBalancer', {}).get('ingress', [])
        if ingress:
            if len(ingress) != 1 or ingress[0].get('ip') != expected_ip:
                raise ValueError('Load balancer did not attach the reserved IP')
            return
        if attempt % 6 == 0:
            print('Waiting for AKS to attach the reserved public IP...', flush=True)
        sleep(5)
    raise TimeoutError('Load balancer assignment timed out; inspect Service events')


def deploy(call, endpoint, progress, connect=rotation.forward, sleep=time.sleep):
    progress('check_storage_and_grafana')
    if call(['kubectl', 'get', 'storageclass', 'managed-csi', '-o', 'jsonpath={.provisioner}']) != 'disk.csi.azure.com':
        raise ValueError('Expected managed-csi Azure Disk class')
    call(['kubectl', 'rollout', 'status', 'deployment/grafana', '-n', NS, '--timeout=120s'])
    existing = call(['kubectl', 'get', 'service/grafana-https', '-n', NS, '--ignore-not-found', '-o', 'name'])
    progress('verify_rotated_credentials')
    values = private_credentials(call, ROOT / '.env.grafana')
    with connect() as request:
        credential_gate(request, values, initial=not bool(existing))
    hostname = endpoint['hostname']
    progress('configure_gateway')
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {
        'name': 'grafana-https-config', 'namespace': NS}, 'data': {
            'GRAFANA_HOST': hostname,
            'Caddyfile': (ROOT / 'monitoring/grafana/Caddyfile').read_text(encoding='utf-8')}}
    call(['kubectl', 'apply', '-f', '-'], json.dumps(config))
    progress('configure_grafana_url')
    call(['kubectl', 'set', 'env', 'deployment/grafana', '-n', NS,
          'GF_SERVER_ROOT_URL=https://' + hostname + '/', 'GF_SERVER_SERVE_FROM_SUB_PATH=false',
          'GF_SECURITY_COOKIE_SECURE=true', 'GF_SECURITY_COOKIE_SAMESITE=lax',
          'GF_AUTH_ANONYMOUS_ENABLED=false', 'GF_USERS_ALLOW_SIGN_UP=false'])
    call(['kubectl', 'rollout', 'status', 'deployment/grafana', '-n', NS, '--timeout=600s'])
    progress('rollout_gateway')
    prior_gateway = call(['kubectl', 'get', 'deployment/grafana-https', '-n', NS,
                          '--ignore-not-found', '-o', 'name'])
    call(['kubectl', 'apply', '-f', str(ROOT / 'kubernetes/monitoring/grafana-https.yaml')])
    if prior_gateway:
        call(['kubectl', 'rollout', 'restart', 'deployment/grafana-https', '-n', NS])
    call(['kubectl', 'rollout', 'status', 'deployment/grafana-https', '-n', NS, '--timeout=600s'])
    progress('publish_https_service')
    call(['kubectl', 'apply', '-f', str(ROOT / 'kubernetes/monitoring/grafana-https-service.yaml')])
    progress('wait_reserved_ip')
    wait_address(call, endpoint['ip_address'], sleep=sleep)
    return {'result': 'GRAFANA_HTTPS_GATEWAY_DEPLOYED', **endpoint,
            'url': 'https://' + hostname + '/', 'tls_verified': False,
            'note': 'Run the HTTPS check before browser login; pod readiness is not certificate proof.'}


class PublicClient:
    def __init__(self, hostname):
        self.hostname = hostname
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), rotation.NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, method, path, payload=None, scheme='https'):
        if scheme != 'https' and not (scheme == 'http' and method == 'GET' and path == '/' and payload is None):
            raise ValueError('Plain HTTP is restricted to the credential-free redirect probe')
        base = scheme + '://' + self.hostname
        headers = {'Accept': 'application/json'}
        if payload is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': 'https://' + self.hostname})
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
        try:
            response = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.headers, response.read()

    def get(self, path):
        status, _, body = self.raw('GET', path)
        if status != 200:
            raise rotation.ApiFailure(status)
        return json.loads(body)

    def secure_session(self):
        return any(cookie.name == 'grafana_session' and cookie.secure
                   and cookie.path == '/' and cookie.domain.lstrip('.') == self.hostname
                   and any(key.lower() == 'httponly' for key in cookie._rest)
                   for cookie in self.jar)


def public_preflight(client, endpoint, resolve=socket.getaddrinfo):
    addresses = {entry[4][0] for entry in resolve(endpoint['hostname'], 443, socket.AF_INET, socket.SOCK_STREAM)}
    if addresses != {endpoint['ip_address']}:
        raise ValueError('DNS does not resolve to the reserved IP')
    status, headers, _ = client.raw('GET', '/', scheme='http')
    if status not in (301, 302, 307, 308) or headers.get('Location') != 'https://' + endpoint['hostname'] + '/':
        raise ValueError('Expected HTTP to HTTPS redirect')
    if client.get('/api/health').get('database') != 'ok':
        raise ValueError('Grafana health is not ready')
    for path in ['/api/user', '/api/dashboards/uid/trafficxx-operations']:
        if client.raw('GET', path)[0] != 401:
            raise ValueError('Unauthenticated Grafana API is not denied')
    for path in ['/metrics', '/debug/pprof/']:
        if client.raw('GET', path)[0] != 404:
            raise ValueError('Diagnostic routes should not be publicly proxied')


def verify_login(client, values):
    login_attempted = False
    try:
        login_attempted = True
        status, _, body = client.raw('POST', '/login', {
            'user': values['GF_SECURITY_ADMIN_USER'], 'password': values['GF_SECURITY_ADMIN_PASSWORD']})
        if status != 200:
            raise rotation.ApiFailure(status)
        if json.loads(body).get('redirectUrl') not in ('/', 'https://' + client.hostname + '/'):
            raise ValueError('Login redirect escaped the canonical Grafana root')
        if not client.secure_session():
            raise ValueError('Expected Secure HttpOnly session cookie at canonical root')
        rotation.admin_identity(client.get('/api/user'))
        # All protected reads use the cookie jar, not HTTP Basic authentication.
        result = dashboard.verify(client.get)
    finally:
        if login_attempted:
            status, _, _ = client.raw('GET', '/logout')
            if status not in (200, 302, 303):
                raise rotation.ApiFailure(status)
            if client.raw('GET', '/api/user')[0] != 401:
                raise ValueError('Diagnostic session did not log out')
    return result


def check(call, endpoint, progress, sleep=time.sleep, attempts=48, client=None, resolve=socket.getaddrinfo):
    values = private_credentials(call, ROOT / '.env.grafana')
    client = client or PublicClient(endpoint['hostname'])
    last = None
    progress('wait_dns_and_trusted_https')
    for attempt in range(attempts):
        try:
            public_preflight(client, endpoint, resolve)
            break
        except (OSError, ValueError, rotation.ApiFailure) as error:
            last = error
            if attempt % 6 == 0:
                print('Waiting for DNS, trusted TLS and protected API readiness...', flush=True)
            sleep(5)
    else:
        raise last or TimeoutError('HTTPS readiness timed out')
    progress('verify_https_cookie_login')
    result = verify_login(client, values)
    return {'result': 'GRAFANA_HTTPS_OK', **endpoint, 'url': 'https://' + endpoint['hostname'] + '/',
            'certificate_verified': True, 'http_redirect_verified': True,
            'cookie_session_authenticated': True, 'session_cookie_secure_httponly': True,
            'unauthenticated_dashboard_denied': True, 'diagnostic_session_logged_out': True,
            'panel_count': result['panel_count'], 'datasource_up': 1,
            'notification_delivery_configured': False}


def main(progress):
    if len(sys.argv) != 2 or sys.argv[1] not in ('deploy', 'check'):
        raise ValueError('Use: python3 scripts/grafana-https.py deploy|check')
    call = command
    progress('check_account_context_and_reserved_ip')
    endpoint = preflight(call)
    if sys.argv[1] == 'deploy':
        return deploy(call, endpoint, progress)
    return check(call, endpoint, progress)


if __name__ == '__main__':
    stage = ['start']
    try:
        print(json.dumps(main(lambda value: stage.__setitem__(0, value))))
    except Exception as error:
        print(json.dumps({'result': 'GRAFANA_HTTPS_FAILED', 'stage': stage[0],
                          'error_type': type(error).__name__, 'http_status': getattr(error, 'http_status', None),
                          'note': 'No credential/header values are printed. Keep all PVCs and the Grafana Secret.'}))
        raise SystemExit(1)
