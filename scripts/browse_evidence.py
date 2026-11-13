"""Per-run Kubernetes evidence, independent of pods and the Cloud Shell disk.

The generator can patch only its pre-created ConfigMap. No credentials or
response bodies are stored. Objects deliberately have no Job owner/TTL.
"""
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import ssl
import urllib.request


def name(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{12}', run_id):
        raise ValueError('INVALID_EVIDENCE_RUN')
    return 'browse-evidence-' + run_id


def encoded(row):
    value = json.dumps(row, separators=(',', ':'), allow_nan=False)
    if len(value.encode()) > 100_000:
        raise ValueError('EVIDENCE_TOO_LARGE')
    return value


def prepare(base, run_id, job_name):
    resource = name(run_id)
    metadata = {'name': resource, 'namespace': 'trafficxx',
                'labels': {'trafficxx.io/run-id': run_id}}
    items = [
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata,
         'data': {'identity.json': encoded({'run_id': run_id, 'job': job_name})}},
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': metadata},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'metadata': metadata,
         'rules': [{'apiGroups': [''], 'resources': ['configmaps'],
                    'resourceNames': [resource], 'verbs': ['patch']}]},
        {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding', 'metadata': metadata,
         'subjects': [{'kind': 'ServiceAccount', 'name': resource, 'namespace': 'trafficxx'}],
         'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': resource}},
    ]
    # Create, never apply/adopt an existing record or identity.
    base.kube('create', '-f', '-', data=encoded({'apiVersion': 'v1', 'kind': 'List', 'items': items}))
    return resource


def save(base, run_id, key, row):
    base.kube('patch', 'configmap', name(run_id), '-n', 'trafficxx', '--type=merge',
              '-p', encoded({'data': {key: encoded(row)}}))


def bind(base, job):
    meta = job['metadata']
    run_id = meta['labels']['trafficxx.io/run-id']
    save(base, run_id, 'identity.json', {'run_id': run_id, 'job': meta['name'], 'job_uid': meta['uid']})


def read(base, job, key):
    meta = job['metadata']
    run_id = meta['labels']['trafficxx.io/run-id']
    cm = json.loads(base.kube('get', 'configmap', name(run_id), '-n', 'trafficxx', '-o', 'json'))
    identity = json.loads(cm.get('data', {}).get('identity.json', '{}'))
    if (cm['metadata'].get('labels', {}).get('trafficxx.io/run-id') != run_id
            or identity != {'run_id': run_id, 'job': meta['name'], 'job_uid': meta['uid']}):
        raise ValueError('EVIDENCE_IDENTITY_MISMATCH')
    raw = cm.get('data', {}).get(key)
    if raw is None:
        raise ValueError('EVIDENCE_RECORD_MISSING')
    if len(raw.encode()) > 100_000:
        raise ValueError('EVIDENCE_TOO_LARGE')
    return json.loads(raw)


def publish(key, row):
    """In-pod write with verified TLS; bounded, fail closed if unavailable."""
    resource = os.environ.get('BROWSE_EVIDENCE_CONFIGMAP')
    if not resource:
        return
    run_id = os.environ['BROWSE_EVIDENCE_RUN']
    if resource != name(run_id) or row.get('run_id') != run_id:
        raise ValueError('EVIDENCE_IDENTITY_MISMATCH')
    if key not in ('progress.json', 'summary.json'):
        raise ValueError('INVALID_EVIDENCE_KEY')
    root = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    request = urllib.request.Request(
        'https://kubernetes.default.svc/api/v1/namespaces/trafficxx/configmaps/' + resource,
        data=encoded({'data': {key: encoded({**row, 'captured_utc': datetime.now(timezone.utc).isoformat()})}}).encode(), method='PATCH',
        headers={'Authorization': 'Bearer ' + (root / 'token').read_text().strip(),
                 'Content-Type': 'application/merge-patch+json'})
    # Do not print raw API exceptions (which can contain sensitive context).
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=str(root / 'ca.crt')),
                                    timeout=3) as response:
            response.read(1024)
    except Exception:
        raise RuntimeError('EVIDENCE_WRITE_FAILED') from None
