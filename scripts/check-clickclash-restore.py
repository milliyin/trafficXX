"""Cloud Shell logical restore rehearsal, before the public game is opened."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def safe_outcome(raw):
    expected = {'result': 'CLICKCLASH_LOGICAL_RESTORE_OK', 'players': 1, 'score': 3, 'receipts': 3,
                'image_restored': True, 'session_restored': True, 'audit_sequence_verified': True,
                'scratch_database_removed': True, 'production_writes': False, 'managed_pitr_tested': False}
    if not isinstance(raw, dict):
        raise RuntimeError('Invalid restore result')
    if raw.get('result') == expected['result']:
        if any(type(raw.get(key)) is not type(value) or raw[key] != value for key, value in expected.items()):
            raise RuntimeError('Incomplete restore verification')
        return expected
    stages = {'read_token', 'check_empty_source', 'dump_source_schema', 'create_scratch', 'restore_source_schema',
              'seed_scratch_only', 'dump_fixture', 'clear_scratch_only', 'restore_fixture', 'verify_records',
              'verify_source_untouched', 'cleanup'}
    removed = raw.get('scratch_database_removed')
    return {'result': 'CLICKCLASH_LOGICAL_RESTORE_FAILED',
            'stage': raw.get('stage') if raw.get('stage') in stages else 'restore_execution',
            'scratch_database_removed': removed if type(removed) is bool else None}


def load_release():
    spec = importlib.util.spec_from_file_location('cc_release', ROOT / 'scripts/clickclash-azure.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    return release


def pod_manifest(name, user):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': 'default'}, 'spec': {
        'restartPolicy': 'Never', 'activeDeadlineSeconds': 600, 'terminationGracePeriodSeconds': 10,
        'automountServiceAccountToken': False, 'nodeSelector': {'agentpool': 'apps'},
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999,
                            'seccompProfile': {'type': 'RuntimeDefault'}},
        'volumes': [{'name': 'scratch', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '64Mi'}}],
        'containers': [{'name': 'check', 'image': 'postgres:17', 'command': ['sleep', '600'],
            'env': [{'name': key, 'value': value} for key, value in {
                'PGHOST': 'trafficxx-pg-e8708a85.postgres.database.azure.com', 'PGPORT': '5432',
                'PGUSER': user, 'PGDATABASE': 'postgres', 'PGSSLMODE': 'verify-full',
                'PGSSLROOTCERT': '/scratch/ca.crt', 'PGCONNECT_TIMEOUT': '10',
                'PGOPTIONS': '-c statement_timeout=15000 -c lock_timeout=3000', 'TMPDIR': '/scratch'}.items()],
            'volumeMounts': [{'name': 'scratch', 'mountPath': '/scratch'}],
            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}}]}}


def main():
    release = load_release()
    release.context()
    if release.call(['az', 'ad', 'signed-in-user', 'show', '--query', 'id', '-o', 'tsv']) != 'f7edc6d0-ccf5-46cc-9877-fd012e5e6a87':
        raise ValueError('Wrong lab administrator')
    if release.call(['kubectl', 'get', 'service', 'clickclash-public', '-n', 'trafficxx', '--ignore-not-found', '-o', 'name']):
        raise ValueError('This first-release rehearsal requires the public service to be absent')
    user = release.call(['az', 'ad', 'signed-in-user', 'show', '--query', 'userPrincipalName', '-o', 'tsv'])
    if not user:
        raise ValueError('Missing administrator identity')
    ca = next((path for path in [Path(os.environ.get('TRAFFICXX_CA_BUNDLE', '/etc/ssl/certs/ca-certificates.crt')),
                               Path('/etc/pki/tls/certs/ca-bundle.crt')] if path.is_file()), None)
    if ca is None or b'-----BEGIN CERTIFICATE-----' not in ca.read_bytes():
        raise ValueError('Missing public CA bundle')
    run_id = uuid4().hex[:12]
    scratch = 'clickclash_restore_' + run_id
    pod = 'clickclash-restore-' + run_id
    print(json.dumps({'result': 'CLICKCLASH_RESTORE_STARTED', 'temporary_database': scratch,
                      'temporary_pod': pod, 'note': 'Fixtures are written only to the new temporary database.'}), flush=True)
    created = False
    outcome = None
    try:
        release.call(['kubectl', 'apply', '-f', '-'], json.dumps(pod_manifest(pod, user)))
        created = True
        release.call(['kubectl', 'wait', '-n', 'default', '--for=condition=Ready', 'pod/' + pod, '--timeout=120s'])
        release.call(['kubectl', 'exec', '-n', 'default', '-i', pod, '--', 'sh', '-c',
                      'umask 077; cat > /scratch/ca.crt'], ca.read_text())
        release.call(['kubectl', 'exec', '-n', 'default', '-i', pod, '--', 'sh', '-c',
                      'umask 077; cat > /scratch/drill.sh'], (ROOT / 'scripts/clickclash-restore-drill.sh').read_text())
        token = release.call(['az', 'account', 'get-access-token', '--resource-type', 'oss-rdbms',
                              '--query', 'accessToken', '-o', 'tsv', '--only-show-errors'])
        # Only the token is sent on stdin, never in argv, files or output.
        result = subprocess.run(['kubectl', 'exec', '-n', 'default', '-i', pod, '--', 'sh', '/scratch/drill.sh', scratch],
                                input=token + '\n', text=True, capture_output=True, timeout=570)
        token = None
        try:
            outcome = safe_outcome(json.loads(result.stdout.strip()))
        except (ValueError, AttributeError):
            raise RuntimeError('Restore did not return a structured result') from None
        if result.returncode or outcome.get('result') != 'CLICKCLASH_LOGICAL_RESTORE_OK':
            print(json.dumps({**outcome, 'temporary_database': scratch}))
            return 1
    finally:
        if created:
            release.call(['kubectl', 'delete', 'pod', pod, '-n', 'default', '--wait=true', '--timeout=60s'])
    print(json.dumps({**outcome, 'temporary_pod_removed': True,
                      'note': 'Logical pg_dump/pg_restore test on Azure; not a managed point-in-time restore or disaster-recovery test.'}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'result': 'CLICKCLASH_LOGICAL_RESTORE_FAILED', 'error_type': type(error).__name__,
                          'note': 'No raw errors or credentials printed. Preserve the STARTED database name for cleanup if interrupted.'}))
        sys.exit(1)
