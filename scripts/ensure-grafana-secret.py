"""User-run Cloud Shell credential setup; never print secret contents."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess


def command(args, payload=None):
    result = subprocess.run(args, input=payload, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Grafana credential command failed')
    return result.stdout.strip()


def validate(values):
    if (set(values) != {'GF_SECURITY_ADMIN_USER', 'GF_SECURITY_ADMIN_PASSWORD'}
            or values['GF_SECURITY_ADMIN_USER'] != 'admin'
            or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', values['GF_SECURITY_ADMIN_PASSWORD'])):
        raise ValueError('Unexpected Grafana bootstrap credential format')
    return values


def read_file(path):
    return validate(dict(line.split('=', 1) for line in path.read_text(encoding='utf-8').splitlines() if line))


def ensure(call=command, path=Path('.env.grafana')):
    if path.is_symlink():
        raise ValueError('Credential file must not be a symlink')
    existing = call(['kubectl', 'get', 'secret/grafana-admin', '-n', 'trafficxx-monitoring',
                     '--ignore-not-found', '-o', 'json'])
    if existing:
        data = json.loads(existing)['data']
        values = validate({key: base64.b64decode(value, validate=True).decode('utf-8')
                           for key, value in data.items()})
        if path.exists() and read_file(path) != values:
            raise ValueError('Local bootstrap file differs from existing Secret; inspect before rotating')
    elif path.exists():
        values = read_file(path)
    else:
        values = {'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': secrets.token_urlsafe(36)}
    if not path.exists():
        # Exclusive creation avoids overwriting credentials or following a new symlink.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(''.join(key + '=' + value + '\n' for key, value in values.items()))
    os.chmod(path, 0o600)
    if not existing:
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
                  'metadata': {'name': 'grafana-admin', 'namespace': 'trafficxx-monitoring'},
                  'data': {k: base64.b64encode(v.encode()).decode() for k, v in values.items()}}
        # Secret JSON travels through stdin, not command arguments or a printed dry run.
        call(['kubectl', 'create', '-f', '-'], json.dumps(secret))
    return {'result': 'GRAFANA_CREDENTIALS_READY', 'existing_secret_reused': bool(existing),
            'private_credential_file': str(path), 'password_printed': False}


if __name__ == '__main__':
    try:
        print(json.dumps(ensure()))
    except Exception as error:
        print(json.dumps({'result': 'GRAFANA_CREDENTIAL_SETUP_FAILED', 'error_type': type(error).__name__}))
        raise SystemExit(1)
