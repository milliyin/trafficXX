"""Cloud Shell only: rotate persisted Grafana password, revoke sessions, sync Secret.

Credentials travel in memory/stdin or mode0600 files, never command arguments/logs.
The pending file is retained after failure so rerunning can finish interrupted work.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location(
    'grafana_credentials', Path(__file__).with_name('ensure-grafana-secret.py'))
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class Unauthorized(Exception):
    http_status = 401


class ApiFailure(RuntimeError):
    def __init__(self, status):
        super().__init__('Grafana API request failed')
        self.http_status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to a preview hostname or other redirect.
        return None


def api_client(base):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(method, path, password, payload=None):
        auth = base64.b64encode(('admin:' + password).encode()).decode()
        req = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise Unauthorized() from None
            raise ApiFailure(error.code) from None
    return request


def write_private(path, values):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(''.join(key + '=' + value + '\n' for key, value in values.items()))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def admin_identity(value):
    if (value.get('login') != 'admin' or value.get('isGrafanaAdmin') is not True
            or type(value.get('id')) is not int or value['id'] < 1):
        raise ValueError('Expected the existing Grafana server administrator')
    return value['id']


def rotate(call, request, path=Path('.env.grafana'), progress=lambda stage: None):
    progress('read_secret')
    pending = path.with_name(path.name + '.next')
    if path.is_symlink() or pending.is_symlink():
        raise ValueError('Credential files must not be symlinks')
    secret = json.loads(call(['kubectl', 'get', 'secret/grafana-admin',
                              '-n', 'trafficxx-monitoring', '-o', 'json']))
    old = credentials.validate({key: base64.b64decode(value, validate=True).decode()
                                for key, value in secret['data'].items()})
    progress('read_private_files')
    if path.exists():
        local = credentials.read_file(path)
        os.chmod(path, 0o600)
        # A patch may have succeeded before the pending file was promoted.
        if local != old and not (pending.exists() and credentials.read_file(pending) == old):
            raise ValueError('Local file differs from Secret; inspect before rotating')
    else:
        write_private(path, old)

    if pending.exists():
        new = credentials.read_file(pending)
        os.chmod(pending, 0o600)
        try:
            progress('authenticate_pending_password')
            user_id = admin_identity(request('GET', '/api/user', new['GF_SECURITY_ADMIN_PASSWORD']))
            changed = True
        except Unauthorized:
            changed = False
    else:
        # Validate the actual persisted password before creating a rotation candidate.
        progress('authenticate_current_password')
        user_id = admin_identity(request('GET', '/api/user', old['GF_SECURITY_ADMIN_PASSWORD']))
        new = {'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': secrets.token_urlsafe(48)}
        progress('save_pending_password')
        write_private(pending, new)
        changed = False

    if not changed:
        progress('authenticate_current_password')
        user_id = admin_identity(request('GET', '/api/user', old['GF_SECURITY_ADMIN_PASSWORD']))
        progress('change_persisted_password')
        request('PUT', f'/api/admin/users/{user_id}/password', old['GF_SECURITY_ADMIN_PASSWORD'],
                {'password': new['GF_SECURITY_ADMIN_PASSWORD']})
    progress('verify_new_password')
    if admin_identity(request('GET', '/api/user', new['GF_SECURITY_ADMIN_PASSWORD'])) != user_id:
        raise ValueError('Changed administrator identity')
    if old != new:
        progress('verify_previous_password_rejected')
        try:
            request('GET', '/api/user', old['GF_SECURITY_ADMIN_PASSWORD'])
        except Unauthorized:
            pass
        else:
            raise ValueError('Previous password still authenticates')

    progress('revoke_grafana_sessions')
    request('POST', f'/api/admin/users/{user_id}/logout', new['GF_SECURITY_ADMIN_PASSWORD'], {})
    progress('verify_sessions_revoked')
    remaining = request('GET', f'/api/admin/users/{user_id}/auth-tokens', new['GF_SECURITY_ADMIN_PASSWORD'])
    if remaining != []:
        raise ValueError('Grafana sessions remain after revocation')

    secret['data'] = {key: base64.b64encode(value.encode()).decode() for key, value in new.items()}
    annotations = secret['metadata'].setdefault('annotations', {})
    annotations.pop('kubectl.kubernetes.io/last-applied-configuration', None)
    annotations['trafficxx/password-rotated-at'] = datetime.now(timezone.utc).isoformat()
    # Replace with resourceVersion from GET to reject concurrent Secret edits.
    progress('synchronize_secret')
    call(['kubectl', 'replace', '-f', '-'], json.dumps(secret))
    progress('verify_secret')
    confirmed = json.loads(call(['kubectl', 'get', 'secret/grafana-admin',
                                 '-n', 'trafficxx-monitoring', '-o', 'json']))
    if confirmed['data'] != secret['data']:
        raise ValueError('Secret verification failed; pending credential retained')
    progress('promote_private_file')
    os.replace(pending, path)
    os.chmod(path, 0o600)
    return {'result': 'GRAFANA_PASSWORD_ROTATED', 'sessions_revoked': True,
            'remaining_sessions': 0, 'secret_synchronized': True,
            'private_credential_file': str(path), 'password_printed': False}


@contextmanager
def forward():
    process = subprocess.Popen(
        ['kubectl', 'port-forward', '-n', 'trafficxx-monitoring', '--address=127.0.0.1',
         'service/grafana', '18091:3000'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    events = queue.Queue()

    def drain():
        for line in process.stdout:
            if line.startswith('Forwarding from 127.0.0.1:18091 -> 3000'):
                events.put(True)
        events.put(False)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        if not events.get(timeout=45) or process.poll() is not None:
            raise RuntimeError('Temporary Grafana forward failed')
        yield api_client('http://127.0.0.1:18091')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        reader.join(timeout=2)
        process.stdout.close()


def main(progress=lambda stage: None):
    os.chdir(Path(__file__).resolve().parents[1])
    call = credentials.command
    progress('check_azure_account')
    if call(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85':
        raise ValueError('Select the lab sponsorship subscription')
    progress('check_kubernetes_context')
    if call(['kubectl', 'config', 'current-context']) != 'trafficxx-lab-aks':
        raise ValueError('Select trafficxx-lab-aks context')
    progress('start_temporary_forward')
    with forward() as request:
        return rotate(call, request, progress=progress)


def failure_result(stage, error):
    # Never include exception text, response bodies, HTTP headers or command output.
    return {'result': 'GRAFANA_ROTATION_FAILED', 'stage': stage,
            'error_type': type(error).__name__, 'http_status': getattr(error, 'http_status', None),
            'note': 'Keep .env.grafana.next; rerun to resume. Never paste credential files.'}


if __name__ == '__main__':
    current_stage = ['start']
    try:
        print(json.dumps(main(lambda stage: current_stage.__setitem__(0, stage))))
    except Exception as error:
        print(json.dumps(failure_result(current_stage[0], error)))
        raise SystemExit(1)
