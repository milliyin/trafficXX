"""User-run Cloud Shell release. No Azure credential files are read or printed."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RG = 'trafficxx-lab-rg'
NS = 'trafficxx'
REGISTRY = 'trafficxxacre8708a85'
SERVER = REGISTRY + '.azurecr.io'
SUBSCRIPTION = '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'


class CommandFailure(RuntimeError):
    def __init__(self, operation, exit_code=None, timed_out=False):
        super().__init__('Release command failed')
        self.operation = operation
        self.exit_code = exit_code
        self.timed_out = timed_out


def operation(args):
    # Fixed labels only: never print arbitrary argv, command input or error text.
    for prefix, label in (
        (['az', 'account', 'show'], 'azure_account'),
        (['az', 'acr', 'show'], 'registry_lookup'),
        (['az', 'acr', 'build'], 'acr_build'),
        (['az', 'acr', 'repository', 'show'], 'image_digest_lookup'),
        (['az', 'acr', 'task', 'list-runs'], 'registry_run_history'),
        (['az', 'network', 'public-ip', 'show'], 'public_ip_lookup'),
        (['az', 'identity', 'show'], 'identity_lookup'),
        (['kubectl', 'config', 'current-context'], 'kubernetes_context'),
        (['kubectl', 'apply'], 'apply_manifests'),
        (['kubectl', 'get', 'service', 'clickclash-public'], 'public_service_lookup'),
        (['kubectl', 'rollout', 'status'], 'deployment_rollout'),
    ):
        if args[:len(prefix)] == prefix:
            return label
    return 'release_command'


def call(args, payload=None):
    label = operation(args)
    try:
        result = subprocess.run(args, input=payload, text=True, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise CommandFailure(label, timed_out=True) from None
    except FileNotFoundError:
        raise CommandFailure(label, exit_code=127) from None
    if result.returncode:
        raise CommandFailure(label, exit_code=result.returncode)
    return result.stdout.strip()


def context(require_cluster=True):
    if call(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != SUBSCRIPTION:
        raise ValueError('Select the trafficXX sponsorship subscription')
    if require_cluster and call(['kubectl', 'config', 'current-context']) != 'trafficxx-lab-aks':
        raise ValueError('Select trafficxx-lab-aks')


def failure_report(error):
    result = {'result': 'CLICKCLASH_RELEASE_FAILED', 'error_type': type(error).__name__,
              'note': 'No credential values or raw command errors are printed. No existing services, credentials or scores are reset.'}
    if isinstance(error, CommandFailure):
        result.update(command_operation=error.operation, command_exit_code=error.exit_code,
                      command_timed_out=error.timed_out)
    elif isinstance(error, ValueError):
        # Only these exact locally defined preflight failures are public.
        checks = {'Select the trafficXX sponsorship subscription': 'WRONG_SUBSCRIPTION',
                  'Select trafficxx-lab-aks': 'WRONG_KUBERNETES_CONTEXT'}
        result['check'] = checks.get(str(error), 'RELEASE_VALIDATION_FAILED')
    return result


def metadata():
    value = json.loads(call(['az', 'network', 'public-ip', 'show', '-g', RG, '-n', 'trafficxx-lab-clickclash-ip', '-o', 'json']))
    host = value['dnsSettings']['fqdn']
    if not re.fullmatch(r'clickclash-e8708a85(?:\.[a-z0-9-]+)?\.eastus\.(?:sysgen\.)?cloudapp\.azure\.com', host):
        raise ValueError('Unexpected ClickClash hostname')
    if value['location'] != 'eastus' or not ipaddress.ip_address(value['ipAddress']).is_global:
        raise ValueError('Unexpected ClickClash public address')
    identity = json.loads(call(['az', 'identity', 'show', '-g', RG, '-n', 'trafficxx-lab-clickclash', '-o', 'json']))
    for key in ('clientId', 'principalId', 'tenantId'):
        if not re.fullmatch(r'[0-9a-f-]{36}', identity[key]):
            raise ValueError('Invalid identity metadata')
    return host, value['ipAddress'], identity


def registration_sql(identity):
    sql = (ROOT / 'database/clickclash/001-initial.sql').read_text()
    checksum = hashlib.sha256(sql.encode()).hexdigest()
    extra_sql = (ROOT / 'database/clickclash/002-blob-banners.sql').read_text(encoding='utf-8')
    extra_checksum = hashlib.sha256(extra_sql.encode()).hexdigest()
    return f"""-- Reviewed ClickClash-only registration. Run using the existing administrator helper.
BEGIN;
SELECT pg_advisory_xact_lock(8432001);
DO $registration$
DECLARE mapping record;
BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trafficxx-lab-clickclash') THEN
    SELECT * INTO mapping FROM pg_catalog.pgaadauth_list_principals(false) WHERE rolname='trafficxx-lab-clickclash';
    IF NOT FOUND OR mapping.objectid IS DISTINCT FROM '{identity['principalId']}'
       OR mapping.tenantid IS DISTINCT FROM '{identity['tenantId']}' OR mapping.principaltype IS DISTINCT FROM 'service'
       OR mapping.isadmin IS DISTINCT FROM 0 THEN
      RAISE EXCEPTION 'Unexpected existing ClickClash role';
    END IF;
  ELSE
    PERFORM pg_catalog.pgaadauth_create_principal_with_oid('trafficxx-lab-clickclash','{identity['principalId']}','service',false,false);
  END IF;
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trafficxx-lab-clickclash' AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolreplication))
     OR pg_has_role('trafficxx-lab-clickclash','azure_pg_admin','MEMBER') THEN
    RAISE EXCEPTION 'ClickClash runtime cannot be an administrator';
  END IF;
END $registration$;
COMMIT;
\\connect trafficxx
BEGIN;
SELECT pg_advisory_xact_lock(8432001);
CREATE TABLE IF NOT EXISTS public.clickclash_migrations(version text PRIMARY KEY, checksum text NOT NULL);
DO $bootstrap$
DECLARE old_checksum text;
BEGIN
  SELECT checksum INTO old_checksum FROM public.clickclash_migrations WHERE version='001-initial.sql';
  IF FOUND THEN
    IF old_checksum <> '{checksum}' THEN RAISE EXCEPTION 'ClickClash schema checksum changed'; END IF;
  ELSE
    EXECUTE $migration${sql}$migration$;
    INSERT INTO public.clickclash_migrations VALUES ('001-initial.sql','{checksum}');
  END IF;
END $bootstrap$;
DO $blob_migration$
DECLARE old_checksum text;
BEGIN
  SELECT checksum INTO old_checksum FROM public.clickclash_migrations WHERE version='002-blob-banners.sql';
  IF FOUND THEN
    IF old_checksum <> '{extra_checksum}' THEN RAISE EXCEPTION 'ClickClash blob migration checksum changed'; END IF;
  ELSE
    EXECUTE $migration${extra_sql}$migration$;
    INSERT INTO public.clickclash_migrations VALUES ('002-blob-banners.sql','{extra_checksum}');
  END IF;
END $blob_migration$;
GRANT CONNECT ON DATABASE trafficxx TO "trafficxx-lab-clickclash";
GRANT USAGE ON SCHEMA clickclash TO "trafficxx-lab-clickclash";
GRANT SELECT ON clickclash.players,clickclash.sessions,clickclash.receipts,clickclash.images,clickclash.control,clickclash.buckets,clickclash.reports TO "trafficxx-lab-clickclash";
GRANT INSERT,UPDATE ON clickclash.players,clickclash.receipts TO "trafficxx-lab-clickclash";
GRANT INSERT,UPDATE,DELETE ON clickclash.sessions,clickclash.images,clickclash.buckets,clickclash.reports TO "trafficxx-lab-clickclash";
GRANT SELECT,INSERT,UPDATE,DELETE ON clickclash.image_blobs TO "trafficxx-lab-clickclash";
GRANT UPDATE(image_bytes) ON clickclash.control TO "trafficxx-lab-clickclash";
DO $scope$
BEGIN
  IF has_schema_privilege('trafficxx-lab-clickclash','public','CREATE')
     OR has_database_privilege('trafficxx-lab-clickclash','trafficxx','CREATE')
     OR has_schema_privilege('trafficxx-lab-clickclash','clickclash','CREATE') THEN
    RAISE EXCEPTION 'ClickClash cannot have DDL privileges';
  END IF;
END $scope$;
COMMIT;
SELECT 'CLICKCLASH_SCHEMA_AND_GRANTS_OK';
"""


def deployment(host, address, identity, image):
    labels = {'app.kubernetes.io/name': 'clickclash'}
    security = {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}}
    container_security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    objects = [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': 'clickclash', 'namespace': NS,
          'annotations': {'azure.workload.identity/client-id': identity['clientId'], 'azure.workload.identity/tenant-id': identity['tenantId']}}, 'automountServiceAccountToken': False},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'clickclash', 'namespace': NS}, 'spec': {
          'replicas': 2, 'revisionHistoryLimit': 3, 'selector': {'matchLabels': labels}, 'template': {
            'metadata': {'labels': {**labels, 'azure.workload.identity/use': 'true'}}, 'spec': {
              'serviceAccountName': 'clickclash', 'automountServiceAccountToken': False, 'nodeSelector': {'agentpool': 'apps'},
              'topologySpreadConstraints': [{'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname', 'whenUnsatisfiable': 'ScheduleAnyway', 'labelSelector': {'matchLabels': labels}}],
              'securityContext': security, 'containers': [{'name': 'app', 'image': image, 'ports': [{'containerPort': 8000}],
                'env': [{'name': key, 'value': value} for key, value in {
                  'DATABASE_AUTH_MODE': 'workload_identity', 'PGHOST': 'trafficxx-pg-e8708a85.postgres.database.azure.com',
                  'PGDATABASE': 'trafficxx', 'PGUSER': 'trafficxx-lab-clickclash', 'CLICKCLASH_POOL_MAX': '4',
                  'CLICKCLASH_ORIGIN': 'https://' + host, 'CLICKCLASH_TRUSTED_PROXY_NETWORK': '10.244.0.0/16',
                  'CLICKCLASH_BLOB_ACCOUNT_URL': 'https://trafficxxcce8708a85.blob.core.windows.net'}.items()],
                'securityContext': container_security, 'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                'startupProbe': {'httpGet': {'path': '/readyz', 'port': 8000}, 'periodSeconds': 5, 'failureThreshold': 24},
                'readinessProbe': {'httpGet': {'path': '/readyz', 'port': 8000}, 'periodSeconds': 10, 'timeoutSeconds': 5},
                'livenessProbe': {'httpGet': {'path': '/healthz', 'port': 8000}, 'periodSeconds': 15}}]}}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'clickclash', 'namespace': NS},
          'spec': {'selector': labels, 'ports': [{'port': 8000, 'targetPort': 8000}]}},
        {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'clickclash-certificates', 'namespace': NS},
          'spec': {'accessModes': ['ReadWriteOnce'], 'storageClassName': 'managed-csi', 'resources': {'requests': {'storage': '5Gi'}}}},
    ]
    caddy = f"""{{
  admin off
  http_port 8080
  https_port 8443
}}
{host} {{
  request_body {{
    max_size 1MB
  }}
  header Strict-Transport-Security "max-age=31536000"
  reverse_proxy clickclash:8000 {{
    header_up X-CC-Client-IP {{remote_host}}
  }}
}}
:8085 {{
  respond /healthz "ok" 200
}}
"""
    objects.append({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'clickclash-gateway', 'namespace': NS}, 'data': {'Caddyfile': caddy}})
    mounts = [{'name': 'certificates', 'mountPath': '/data'}, {'name': 'config', 'mountPath': '/etc/caddy', 'readOnly': True},
              {'name': 'runtime', 'mountPath': '/runtime'}, {'name': 'temp', 'mountPath': '/tmp'}, {'name': 'runtime-config', 'mountPath': '/config'}]
    gateway = {'name': 'gateway', 'image': 'caddy:2.11.7-alpine', 'securityContext': container_security,
               'env': [{'name': 'XDG_DATA_HOME', 'value': '/data'}, {'name': 'XDG_CONFIG_HOME', 'value': '/config'}],
               'volumeMounts': mounts, 'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'}, 'limits': {'cpu': '1', 'memory': '256Mi'}}}
    objects.extend([
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'clickclash-gateway', 'namespace': NS}, 'spec': {
          'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': {'app': 'clickclash-gateway'}}, 'template': {
            'metadata': {'labels': {'app': 'clickclash-gateway'}}, 'spec': {
              'automountServiceAccountToken': False, 'nodeSelector': {'agentpool': 'apps'}, 'securityContext': {**security, 'fsGroup': 10001},
              'volumes': [{'name': 'certificates', 'persistentVolumeClaim': {'claimName': 'clickclash-certificates'}},
                {'name': 'config', 'configMap': {'name': 'clickclash-gateway'}}, {'name': 'runtime', 'emptyDir': {'sizeLimit': '64Mi'}},
                {'name': 'temp', 'emptyDir': {'sizeLimit': '32Mi'}}, {'name': 'runtime-config', 'emptyDir': {'sizeLimit': '16Mi'}}],
              'initContainers': [{**gateway, 'name': 'validate', 'command': ['/bin/sh', '-ec', 'cat /usr/bin/caddy > /runtime/caddy; chmod 0555 /runtime/caddy; /runtime/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile']}],
              'containers': [{**gateway, 'command': ['/runtime/caddy', 'run', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'],
                 'readinessProbe': {'httpGet': {'path': '/healthz', 'port': 8085}, 'periodSeconds': 5},
                 'livenessProbe': {'httpGet': {'path': '/healthz', 'port': 8085}, 'periodSeconds': 15}}]}}}},
        {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'clickclash-public', 'namespace': NS,
          'annotations': {'service.beta.kubernetes.io/azure-load-balancer-resource-group': RG, 'service.beta.kubernetes.io/azure-pip-name': 'trafficxx-lab-clickclash-ip'}},
          'spec': {'type': 'LoadBalancer', 'loadBalancerIP': address, 'selector': {'app': 'clickclash-gateway'},
                   'ports': [{'name': 'http', 'port': 80, 'targetPort': 8080}, {'name': 'https', 'port': 443, 'targetPort': 8443}]}}
    ])
    return {'apiVersion': 'v1', 'kind': 'List', 'items': objects}


def private_stage(manifests):
    items = [item for item in manifests['items'] if item['metadata']['name'] == 'clickclash'
             and item['kind'] in ('ServiceAccount', 'Deployment', 'Service')]
    if {item['kind'] for item in items} != {'ServiceAccount', 'Deployment', 'Service'} or len(items) != 3:
        raise ValueError('Invalid private-stage manifest')
    if any(item['kind'] == 'Service' and item['spec'].get('type', 'ClusterIP') != 'ClusterIP' for item in items):
        raise ValueError('Private stage cannot publish a load balancer')
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def storage_preflight():
    account = json.loads(call(['az', 'storage', 'account', 'show', '-g', RG, '-n', 'trafficxxcce8708a85',
        '--query', '{keys:allowSharedKeyAccess,public:allowBlobPublicAccess,tls:minimumTlsVersion,https:enableHttpsTrafficOnly,blob:primaryEndpoints.blob}', '-o', 'json']))
    if (account.get('keys') is not False or account.get('public') is not False
            or account.get('tls') not in ('TLS1_2', 'TLS1_3') or account.get('https') is not True
            or account.get('blob') != 'https://trafficxxcce8708a85.blob.core.windows.net/'):
        raise ValueError('Unsafe banner storage configuration')
    url = ('https://management.azure.com/subscriptions/' + SUBSCRIPTION + '/resourceGroups/' + RG
           + '/providers/Microsoft.Storage/storageAccounts/trafficxxcce8708a85/blobServices/default/containers/banners?api-version=2023-05-01')
    access = call(['az', 'rest', '--method', 'get', '--url', url, '--query', 'properties.publicAccess', '-o', 'tsv'])
    if access.lower() not in ('none', ''):
        raise ValueError('Banner container must be private')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['doctor', 'build', 'prepare', 'stage', 'deploy', 'check', 'migrate-blobs', 'check-blobs', 'cleanup-blobs'])
    args = parser.parse_args()
    context(require_cluster=args.action not in ('doctor', 'build'))
    if args.action == 'doctor':
        registry = call(['az', 'acr', 'show', '--name', REGISTRY, '--query', 'loginServer', '-o', 'tsv'])
        if registry != SERVER:
            raise ValueError('Unexpected container registry')
        runs = json.loads(call(['az', 'acr', 'task', 'list-runs', '--registry', REGISTRY,
                                '--query', '[:3].{run_id:runId,status:status,created_at:createTime}', '-o', 'json']))
        print(json.dumps({'result': 'CLICKCLASH_BUILD_PREFLIGHT_OK', 'registry': registry,
                          'kubernetes_context_required_for_build': False, 'recent_builds': runs, 'read_only': True}))
        return
    if args.action == 'build':
        tag = 'clickclash-' + time.strftime('%Y%m%d%H%M%S', time.gmtime())
        call(['az', 'acr', 'build', '--registry', REGISTRY, '--image', 'trafficxx:' + tag, '--file', 'Dockerfile.clickclash', str(ROOT)])
        digest = call(['az', 'acr', 'repository', 'show', '--name', REGISTRY, '--image', 'trafficxx:' + tag, '--query', 'digest', '-o', 'tsv'])
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Invalid image digest')
        (ROOT / 'clickclash-image.txt').write_text(SERVER + '/trafficxx@' + digest + '\n')
        print('CLICKCLASH_IMAGE_BUILD_OK')
        return
    host, address, identity = metadata()
    if args.action in ('migrate-blobs', 'check-blobs', 'cleanup-blobs'):
        operation = {'migrate-blobs': 'migrate', 'check-blobs': 'check', 'cleanup-blobs': 'cleanup'}[args.action]
        # Existing serving pod identity only. Cleanup is dry-run from this release helper.
        result = call(['kubectl', 'exec', '-n', NS, 'deployment/clickclash', '-c', 'app', '--',
                       'python', '-m', 'trafficxx.clickclash_blob_maintenance', operation])
        value = json.loads(result)
        print(json.dumps({key: value[key] for key in ('result', 'moved', 'remaining', 'verified', 'legacy_remaining', 'candidates', 'removed', 'dry_run', 'next_cursor', 'deleted_account_images_unlinked') if key in value}))
        return
    if args.action == 'prepare':
        target = ROOT / 'test-results/clickclash/azure-register.sql'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(registration_sql(identity))
        print(json.dumps({'result': 'CLICKCLASH_SQL_PREPARED', 'review_file': str(target), 'url': 'https://' + host}))
    elif args.action in ('stage', 'deploy'):
        storage_preflight()
        image = (ROOT / 'clickclash-image.txt').read_text().strip()
        if not re.fullmatch(re.escape(SERVER) + r'/trafficxx@sha256:[0-9a-f]{64}', image):
            raise ValueError('Build and save the reviewed image digest first')
        manifests = deployment(host, address, identity, image)
        if args.action == 'stage':
            if call(['kubectl', 'get', 'service', 'clickclash-public', '-n', NS, '--ignore-not-found', '-o', 'name']):
                raise ValueError('Public ClickClash service already exists; private staging is not a withdrawal action')
            manifests = private_stage(manifests)
        call(['kubectl', 'apply', '-f', '-'], json.dumps(manifests))
        for name in (('clickclash',) if args.action == 'stage' else ('clickclash', 'clickclash-gateway')):
            print('Waiting for deployment/' + name, flush=True)
            call(['kubectl', 'rollout', 'status', 'deployment/' + name, '-n', NS, '--timeout=600s'])
        if args.action == 'stage':
            print(json.dumps({'result': 'CLICKCLASH_STAGED_PRIVATE', 'rollout_verified': True, 'public_listener_created': False,
                              'note': 'Only the application, service account and ClusterIP service were applied. No clicks were generated.'}))
        else:
            print(json.dumps({'result': 'CLICKCLASH_DEPLOYED', 'url': 'https://' + host, 'tls_verified': False}))
    else:
        for path in ('/', '/readyz', '/api/leaderboard'):
            with urllib.request.urlopen('https://' + host + path, timeout=20) as response:
                body = response.read(262144)
                if response.status != 200:
                    raise ValueError('Public route unavailable')
                if path == '/' and b'ClickClash' not in body:
                    raise ValueError('Wrong public website')
                if path == '/readyz' and json.loads(body).get('readiness') != 'ready':
                    raise ValueError('Readiness unavailable')
                if path == '/api/leaderboard' and not isinstance(json.loads(body).get('entries'), list):
                    raise ValueError('Leaderboard unavailable')
        print(json.dumps({'result': 'CLICKCLASH_HTTPS_OK', 'url': 'https://' + host, 'certificate_verified': True,
                          'note': 'Public read routes only; browser signup/scoring and restore rehearsal remain release checks.'}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps(failure_report(error)))
        sys.exit(1)
