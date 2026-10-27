"""Cloud Shell only: bounded Browse KEDA installation, checks and real-load drill."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
import re
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trial = load('run-browse-pool-comparison')
base = trial.base
F = '{job="trafficxx-browse-pool"}'
M = 'trafficxx_browse_pool_'
# Do not turn missing targets/fields into a zero-pressure downscale signal.
GUARD = (' and on() (min(up' + F + ') == 1)'
         ' and on() (count(up' + F + ') >= 2)')
for field in ('size', 'available', 'waiting', 'borrows_total', 'limit'):
    GUARD += (' and on() (count(' + M + field + F + ') == count(up' + F + '))'
              ' and on() (min(timestamp(' + M + field + F + ')) > time() - 45)')
GUARD += ' and on() (min(' + M + 'limit' + F + ') == 8)'
PRESSURE = ('sum(clamp_min(' + M + 'size' + F + ' - ' + M + 'available' + F + ', 0) + '
            + M + 'waiting' + F + ')' + GUARD)
BORROWS = ('sum(rate(' + M + 'borrows_total' + F + '[1m]))' + GUARD +
           ' and on() (count(rate(' + M + 'borrows_total' + F + '[1m])) == count(up' + F + '))')
JOB = 'browse-users-250-autoscaling'


def require(condition, code):
    if not condition:
        raise ValueError(code)


def emit(result, **values):
    print(json.dumps({'result': result, **values}), flush=True)


def command_operation(command):
    """Return only fixed operation labels, never argv, patches, tokens or output."""
    if not isinstance(command, (list, tuple)) or not all(isinstance(v, str) for v in command):
        return 'unknown'
    args = list(command)
    if args and args[0] == 'kubectl':
        args = args[1:]
        if args and args[0] == '--request-timeout=20s':
            args = args[1:]
        if args[:3] == ['get', '--raw', '/apis/metrics.k8s.io/v1beta1/namespaces/trafficxx/pods']:
            return 'read_pod_resource_metrics'
        for resource, label in (('deployment', 'deployment'), ('hpa', 'hpa'),
                                ('scaledobject', 'scaledobject'), ('job', 'job'),
                                ('jobs', 'jobs'), ('pods', 'pods'), ('node', 'node')):
            if args[:2] == ['get', resource]:
                return 'read_' + label
        if args[:2] == ['patch', 'job']:
            return 'stop_owned_load_job'
        if args[:1] == ['exec']:
            return 'release_load_gate'
        if args[:1] == ['logs']:
            return 'read_load_logs'
        return 'other_kubectl_operation'
    if args[:4] == ['az', 'monitor', 'metrics', 'list']:
        return 'read_azure_database_metrics'
    if args[:3] == ['az', 'account', 'show']:
        return 'read_azure_subscription'
    if args[:1] == ['bash']:
        return 'application_health_check'
    return 'unknown'


def failure_details(error):
    check = str(error) if isinstance(error, ValueError) else ''
    return {'error_type': type(error).__name__,
            'check': check if re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', check) else None,
            'command_operation': command_operation(getattr(error, 'cmd', None)),
            'command_exit_code': error.returncode if isinstance(error, subprocess.CalledProcessError) else None,
            'command_timed_out': isinstance(error, subprocess.TimeoutExpired)}


def cleanup_load(job, job_name, *, terminal=False):
    if terminal:
        return 'already_terminal'
    state = json.loads(base.kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
    require(state['metadata']['uid'] == job['metadata']['uid'], 'JOB_REPLACED')
    if any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
           for c in state.get('status', {}).get('conditions', [])):
        return 'already_terminal'
    trial.users.stop_owned_job(job['metadata']['uid'], job_name)
    return 'stop_requested'


def scaled_object(pressure_target='5', scale_up_seconds=60):
    require(pressure_target in ('3', '5'), 'UNREVIEWED_PRESSURE_TARGET')
    require((pressure_target, scale_up_seconds) in (('5', 60), ('3', 60), ('3', 30)),
            'UNREVIEWED_SCALING_PROFILE')
    row = json.loads((ROOT / 'kubernetes/apps/browse-autoscaling.json').read_text())
    row['spec']['advanced']['horizontalPodAutoscalerConfig']['behavior']['scaleUp']['policies'][0]['periodSeconds'] = scale_up_seconds
    for name, query, threshold in (('pool-pressure', PRESSURE, pressure_target), ('borrow-rate', BORROWS, '40')):
        row['spec']['triggers'].append({'type': 'prometheus', 'name': name, 'metricType': 'AverageValue',
            'metadata': {'serverAddress': 'http://prometheus.trafficxx-monitoring.svc.cluster.local:9090',
                         'query': query, 'threshold': threshold, 'ignoreNullValues': 'false'}})
    return row


@contextmanager
def prometheus():
    # Own only this process; never kill an existing port listener.
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen(['kubectl', 'port-forward', '-n', 'trafficxx-monitoring',
            '--address=127.0.0.1', 'service/prometheus', '18094:9090'], stdout=log, stderr=log)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def query(expression):
            path = '/api/v1/query?' + urllib.parse.urlencode({'query': expression})
            with opener.open('http://127.0.0.1:18094' + path, timeout=10) as response:
                payload = json.load(response)
            require(payload.get('status') == 'success', 'PROMETHEUS_QUERY_FAILED')
            return payload['data']['result']

        try:
            for attempt in range(40):
                require(process.poll() is None, 'PROMETHEUS_FORWARD_EXITED')
                log.seek(0)
                if 'Forwarding from 127.0.0.1:18094' in log.read():
                    break
                time.sleep(.25)
            else:
                raise ValueError('PROMETHEUS_FORWARD_NOT_READY')
            query('vector(1)')
            yield query
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def scalar(query, expression):
    rows = query(expression)
    require(len(rows) == 1, 'SCALING_METRIC_MISSING')
    value = float(rows[0]['value'][1])
    require(math.isfinite(value) and value >= 0, 'INVALID_SCALING_METRIC')
    return value


def metric_active(hpa):
    return any(c['type'] == 'ScalingActive' and c['status'] == 'True'
               for c in hpa.get('status', {}).get('conditions', []))


class MetricReadiness:
    """Bounded observation grace; never substitute zero for an HPA failure."""
    def __init__(self):
        self.since, self.gaps, self.total, self.maximum = None, 0, 0.0, 0.0

    def observe(self, hpa, now):
        if metric_active(hpa):
            if self.since is not None:
                elapsed = now - self.since
                require(elapsed < 60 and self.total + elapsed < 60, 'HPA_METRIC_GAP_TIMEOUT')
                self.total += elapsed
                self.maximum = max(self.maximum, elapsed)
                emit('BROWSE_HPA_METRIC_RECOVERED', gap_seconds=round(elapsed, 3))
                self.since = None
            return
        if self.since is None:
            self.since = now
            self.gaps += 1
            emit('BROWSE_HPA_METRIC_GAP', grace_seconds=60,
                 note='No zero metric substitution. Resource, HTTP and database checks continue.')
        require(now - self.since < 60 and self.total + now - self.since < 60, 'HPA_METRIC_GAP_TIMEOUT')

    def summary(self):
        require(self.since is None, 'HPA_METRIC_GAP_UNRESOLVED')
        return {'gap_count': self.gaps, 'total_gap_seconds': round(self.total, 3),
                'maximum_gap_seconds': round(self.maximum, 3)}


class PoolWaitingWatch:
    """Hold evidence comes from distinct source samples on the same instance."""
    def __init__(self):
        self.instances = {}
        self.not_before = None

    def observe(self, rows, now):
        require(2 <= len(rows) <= 8, 'POOL_WAITING_SERIES_MISSING')
        seen = set()
        for row in rows:
            instance = row.get('metric', {}).get('instance', '')
            require(re.fullmatch(r'[A-Za-z0-9.\[\]:-]{1,150}', instance) is not None
                    and instance not in seen, 'INVALID_POOL_WAITING_INSTANCE')
            seen.add(instance)
            samples = row.get('values')
            require(isinstance(samples, list) and 0 < len(samples) <= 32, 'POOL_WAITING_SAMPLES_MISSING')
            parsed, previous = [], -math.inf
            for stamp, value in samples:
                stamp, value = float(stamp), float(value)
                require(math.isfinite(stamp) and previous < stamp <= now + 5
                        and stamp >= now - 60 and math.isfinite(value) and value >= 0,
                        'INVALID_POOL_WAITING_SAMPLE')
                previous = stamp
                parsed.append((stamp, value))
            require(now - parsed[-1][0] <= 45, 'POOL_WAITING_SAMPLE_STALE')
            for stamp, value in parsed:
                if self.not_before is not None and stamp < self.not_before:
                    continue
                state = self.instances.get(instance)
                if state is not None and stamp <= state['last']:
                    require(stamp != state['last'] or value == state['value'], 'POOL_WAITING_SAMPLE_CHANGED')
                    continue
                if state is not None and state['value'] > 0:
                    require(stamp - state['last'] <= 30, 'POOL_WAITING_SAMPLE_GAP')
                since = (state['since'] if state is not None and state['value'] > 0 else stamp) if value > 0 else None
                self.instances[instance] = {'last': stamp, 'value': value, 'since': since,
                    'samples': (state['samples'] if state is not None else 0) + 1,
                    'maximum': max(value, state['maximum'] if state is not None else 0)}
                if since is not None:
                    require(stamp - since < 10, 'SUSTAINED_POOL_WAITING')
        # Missing/stale series are never replaced by a zero or a recovery.
        require(set(self.instances) <= seen, 'POOL_WAITING_SERIES_MISSING')

    def summary(self):
        return {instance: {'distinct_source_samples': state['samples'], 'last_waiting': state['value'],
                'last_source_timestamp_utc': datetime.fromtimestamp(state['last'], timezone.utc).isoformat(),
                'positive_since_utc': datetime.fromtimestamp(state['since'], timezone.utc).isoformat()
                    if state['since'] is not None else None, 'sampled_peak_waiting': state['maximum']}
                for instance, state in self.instances.items()}


def owned_hpa(*, require_active=True, pressure_target=None, scale_up_seconds=None):
    rows = json.loads(base.kube('get', 'hpa', '-n', 'trafficxx', '-o', 'json'))['items']
    matches = [r for r in rows if r['spec']['scaleTargetRef'].get('name') == 'browse']
    require(len(matches) == 1 and matches[0]['metadata']['name'] == 'trafficxx-browse', 'EXPECTED_KEDA_HPA')
    so = json.loads(base.kube('get', 'scaledobject', 'browse', '-n', 'trafficxx', '-o', 'json'))
    require(not any(k.startswith('autoscaling.keda.sh/paused') for k in so['metadata'].get('annotations', {})),
            'AUTOSCALING_PAUSED')
    targets = (pressure_target,) if pressure_target is not None else ('5', '3')
    profiles = [(t, seconds) for t, seconds in (('5', 60), ('3', 60), ('3', 30))
                if t in targets and (scale_up_seconds is None or seconds == scale_up_seconds)]
    require(any(contains(so['spec'], scaled_object(t, seconds)['spec']) for t, seconds in profiles),
            'AUTOSCALING_POLICY_CHANGED')
    require(any(r.get('uid') == so['metadata']['uid'] and r.get('controller')
                for r in matches[0]['metadata'].get('ownerReferences', [])), 'FOREIGN_HPA')
    require(matches[0]['spec']['minReplicas'] == 2 and matches[0]['spec']['maxReplicas'] == 4,
            'HPA_BOUNDS_CHANGED')
    if pressure_target is not None:
        require_hpa_targets(matches[0], pressure_target)
    if scale_up_seconds is not None:
        expected = scaled_object('3', scale_up_seconds)['spec']['advanced']['horizontalPodAutoscalerConfig']['behavior']
        require(contains(matches[0]['spec'].get('behavior', {}), expected), 'HPA_BEHAVIOR_NOT_RECONCILED')
    require(not require_active or metric_active(matches[0]), 'HPA_METRIC_NOT_ACTIVE')
    return matches[0]


def require_hpa_targets(hpa, pressure_target):
    metrics = hpa['spec'].get('metrics', [])
    targets = {m['external']['metric']['name']: m['external']['target']
               for m in metrics if m.get('type') == 'External'}
    require(len(metrics) == 2 and targets == {
        's0-prometheus': {'type': 'AverageValue', 'averageValue': pressure_target},
        's1-prometheus': {'type': 'AverageValue', 'averageValue': '40'}}, 'HPA_TARGET_NOT_RECONCILED')


def configure_early():
    """Change only the reviewed pressure threshold; safely resume after reconciliation delays."""
    trial.context()
    trial.no_active_load()
    trial.health()
    database_pressure()
    with prometheus() as query:
        app = wait_baseline(query)
        if app['spec']['replicas'] != 2:
            app = wait_until(lambda: idle_two(app['metadata']['uid'], query), 600)
        owned_hpa(scale_up_seconds=60)
        so = json.loads(base.kube('get', 'scaledobject', 'browse', '-n', 'trafficxx', '-o', 'json'))
        require(not any(k.startswith('autoscaling.keda.sh/paused')
                        for k in so['metadata'].get('annotations', {})), 'AUTOSCALING_PAUSED')
        old = so['spec']['triggers'][0]['metadata']['threshold']
        require(old in ('5', '3') and contains(so['spec'], scaled_object(old)['spec']),
                'AUTOSCALING_POLICY_CHANGED')
        # Check ownership again against the exact snapshot being patched.
        hpa = owned_hpa(scale_up_seconds=60)
        require(any(r.get('uid') == so['metadata']['uid'] and r.get('controller')
                    for r in hpa['metadata'].get('ownerReferences', [])), 'FOREIGN_HPA')
        if old == '5':
            operations = [
                {'op': 'test', 'path': '/metadata/uid', 'value': so['metadata']['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': so['metadata']['resourceVersion']},
                {'op': 'test', 'path': '/spec', 'value': so['spec']},
                {'op': 'replace', 'path': '/spec/triggers/0/metadata/threshold', 'value': '3'}]
            base.kube('patch', 'scaledobject', 'browse', '-n', 'trafficxx', '--type=json',
                      '-p', json.dumps(operations))
        def reconciled():
            return owned_hpa(pressure_target='3', scale_up_seconds=60)
        wait_until(reconciled, 180)
        deployment(app['metadata']['uid'])
        check(query)
    emit('BROWSE_EARLY_SCALING_CONFIGURED', pressure_target_per_pod=3, borrow_rate_target_per_pod=40,
         min_replicas=2, max_replicas=4, pool_per_pod=8, queue_per_pod=16,
         inventory_writes=False, note='Earlier scaling may use extra pods sooner. No load was started.')


def configure_fast_scale_up():
    """Compare a shorter scale-up policy period without changing capacity bounds."""
    trial.context()
    trial.no_active_load()
    trial.health()
    database_pressure()
    with prometheus() as query:
        app = wait_baseline(query)
        if app['spec']['replicas'] != 2:
            app = wait_until(lambda: idle_two(app['metadata']['uid'], query), 600)
        hpa = owned_hpa(pressure_target='3')
        so = json.loads(base.kube('get', 'scaledobject', 'browse', '-n', 'trafficxx', '-o', 'json'))
        require(not any(k.startswith('autoscaling.keda.sh/paused')
                        for k in so['metadata'].get('annotations', {})), 'AUTOSCALING_PAUSED')
        require(any(r.get('uid') == so['metadata']['uid'] and r.get('controller')
                    for r in hpa['metadata'].get('ownerReferences', [])), 'FOREIGN_HPA')
        behavior = so['spec']['advanced']['horizontalPodAutoscalerConfig']['behavior']
        old = behavior['scaleUp']['policies'][0]['periodSeconds']
        require(old in (60, 30) and contains(so['spec'], scaled_object('3', old)['spec']),
                'AUTOSCALING_POLICY_CHANGED')
        if old == 60:
            operations = [
                {'op': 'test', 'path': '/metadata/uid', 'value': so['metadata']['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': so['metadata']['resourceVersion']},
                {'op': 'test', 'path': '/spec', 'value': so['spec']},
                {'op': 'replace', 'path': '/spec/advanced/horizontalPodAutoscalerConfig/behavior/scaleUp/policies/0/periodSeconds',
                 'value': 30}]
            base.kube('patch', 'scaledobject', 'browse', '-n', 'trafficxx', '--type=json', '-p', json.dumps(operations))
        wait_until(lambda: owned_hpa(pressure_target='3', scale_up_seconds=30), 180)
        deployment(app['metadata']['uid'])
        check(query)
    emit('BROWSE_FAST_SCALE_UP_CONFIGURED', pressure_target_per_pod=3, scale_up_pods_per_step=1,
         scale_up_period_seconds=30, min_replicas=2, max_replicas=4, pool_per_pod=8, queue_per_pod=16,
         inventory_writes=False, note='Allows faster additions when metrics request them; no load was started.')


def contains(actual, expected):
    # API defaulting may add fields, but must not change any reviewed value.
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def database_pressure():
    capacity = load('inspect-browse-capacity')
    end = datetime.now(timezone.utc).replace(microsecond=0)
    payload = json.loads(base.command(['az', 'monitor', 'metrics', 'list', '--resource', capacity.RESOURCE,
        '--metric', 'cpu_percent', 'memory_percent', 'active_connections', '--aggregation', 'Average', 'Maximum',
        '--interval', 'PT1M', '--start-time', (end - timedelta(minutes=5)).isoformat(),
        '--end-time', end.isoformat(), '-o', 'json']))
    rows = capacity.summarize_metrics(payload)
    require(all(row['observations'] > 0 for row in rows.values()), 'DATABASE_METRICS_MISSING')
    require(rows['cpu_percent']['window_maximum'] < 70 and rows['memory_percent']['window_maximum'] < 80
            and rows['active_connections']['window_maximum'] < 112, 'DATABASE_BUDGET_OR_PRESSURE_STOP')
    emit('BROWSE_SCALING_DATABASE_WINDOW', metrics=rows,
         note='Last five minutes, minute aggregation. Includes idle sessions; not a hard connection quota.')
    return rows


def deployment(expected_uid=None):
    app = trial.deployment()
    require(expected_uid is None or app['metadata']['uid'] == expected_uid, 'DEPLOYMENT_REPLACED')
    require(2 <= app['spec']['replicas'] <= 4, 'REPLICA_BUDGET_EXCEEDED')
    container = app['spec']['template']['spec']['containers'][0]
    require(container['name'] == 'browse' and len(app['spec']['template']['spec']['containers']) == 1,
            'UNEXPECTED_BROWSE_PROCESS_COUNT')
    env = {r['name']: r.get('value') for r in container['env']}
    require(all(env.get(k) == '8' for k in ('BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX'))
            and env.get('BROWSE_DB_POOL_MAX_WAITING') == '16', 'POOL_POLICY_CHANGED')
    require(app['spec']['strategy']['rollingUpdate'] == {'maxSurge': 0, 'maxUnavailable': 1},
            'ROLLOUT_BUDGET_CHANGED')
    return app


def check(query):
    app = deployment()
    owned_hpa()
    count = app['spec']['replicas']
    require(app.get('status', {}).get('readyReplicas') == count, 'BROWSE_NOT_READY')
    rows = query('up' + F)
    require(len(rows) == count, 'DISCOVERY_NOT_CONVERGED')
    values = {'pressure': scalar(query, PRESSURE), 'borrows_per_second': scalar(query, BORROWS)}
    emit('BROWSE_AUTOSCALING_OK', replicas=count, pool_per_pod=8, steady_browse_connection_cap=32,
         planning_connection_budget=128, signals=values, notification_delivery_configured=False)
    return app


def wait_baseline(query, seconds=180, *, clock=time.monotonic, sleep=time.sleep):
    """Wait for asynchronous readiness/discovery without relaxing final evidence."""
    transient = {'DISCOVERY_NOT_CONVERGED', 'BROWSE_NOT_READY',
                 'SCALING_METRIC_MISSING', 'HPA_METRIC_NOT_ACTIVE'}
    deadline, previous = clock() + seconds, None
    while True:
        try:
            return check(query)
        except ValueError as error:
            reason = str(error)
            if reason not in transient:
                raise
            require(clock() < deadline, 'BROWSE_BASELINE_CONVERGENCE_TIMEOUT')
            if reason != previous:
                emit('BROWSE_WAITING_FOR_METRIC_BASELINE', check=reason, timeout_seconds=seconds,
                     note='Read-only wait; this check creates no load Job.')
                previous = reason
            sleep(5)


def deploy():
    trial.context()
    trial.no_active_load()
    app = trial.deployment()
    require(app['spec']['replicas'] == 2, 'START_WITH_TWO_REPLICAS')
    # Refuse a foreign HPA, then permit resumable application of our own policy.
    hpas = json.loads(base.kube('get', 'hpa', '-n', 'trafficxx', '-o', 'json'))['items']
    require(not any(r['spec']['scaleTargetRef'].get('name') == 'browse'
                    and r['metadata']['name'] != 'trafficxx-browse' for r in hpas), 'FOREIGN_HPA')
    if any(r['spec']['scaleTargetRef'].get('name') == 'browse' for r in hpas):
        owned_hpa()
    trial.health()
    database_pressure()
    container = app['spec']['template']['spec']['containers'][0]
    require(container['name'] == 'browse' and len(app['spec']['template']['spec']['containers']) == 1,
            'UNEXPECTED_BROWSE_PROCESS_COUNT')
    import re
    require(re.fullmatch(r'trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}', container['image']),
            'IMMUTABLE_IMAGE_REQUIRED')
    changes = [{'op': 'test', 'path': '/metadata/uid', 'value': app['metadata']['uid']},
               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']}]
    for name, value in (('BROWSE_DB_POOL_MIN', '8'), ('BROWSE_DB_POOL_MAX', '8'),
                        ('BROWSE_DB_POOL_MAX_WAITING', '16')):
        matches = [i for i, r in enumerate(container['env']) if r['name'] == name]
        require(len(matches) == 1, 'MISSING_OR_DUPLICATE_POOL_ENV')
        require(container['env'][matches[0]].get('value') in (('4', '8') if value == '8' else ('16',)),
                'UNEXPECTED_POOL_POLICY')
        changes.append({'op': 'replace', 'path': '/spec/template/spec/containers/0/env/' + str(matches[0]) + '/value',
                        'value': value})
    changes.append({'op': 'add', 'path': '/spec/strategy', 'value': {
        'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1}}})
    base.kube('patch', 'deployment', 'browse', '-n', 'trafficxx', '--type=json', '--patch', json.dumps(changes))
    print(base.kube('rollout', 'status', 'deployment/browse', '-n', 'trafficxx', '--timeout=300s', timeout=320), flush=True)
    # Native managed addon; no Helm download, broad custom RBAC, or SQL grants.
    enabled = base.command(['az', 'aks', 'show', '-g', 'trafficxx-lab-rg', '-n', 'trafficxx-lab-aks',
                            '--query', 'workloadAutoScalerProfile.keda.enabled', '-o', 'tsv'])
    if enabled.lower() != 'true':
        emit('BROWSE_KEDA_ENABLING', note='AKS managed addon; this operation may take several minutes.')
        base.command(['az', 'aks', 'update', '-g', 'trafficxx-lab-rg', '-n', 'trafficxx-lab-aks',
                      '--enable-keda', '--only-show-errors', '-o', 'none'], timeout=900)
    base.kube('wait', '--for=condition=Established', 'crd/scaledobjects.keda.sh', '--timeout=180s', timeout=200)
    with prometheus() as query:
        wait_until(lambda: scalar(query, PRESSURE), 180)
        base.kube('apply', '-f', '-', data=json.dumps(scaled_object()))
        wait_until(lambda: check(query), 180)
    trial.health()
    emit('BROWSE_AUTOSCALING_DEPLOYED', normal_pool=8, replicas=[2, 4], queue_limit=16,
         note='Not yet a scale-up/down drill pass. Preserve enable_browse_keda=true in Terraform inputs.')


def wait_until(function, seconds):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return function()
        except (ValueError, KeyError, OSError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)


def create_job(app, run_id, job_name=JOB, *, users=250, slower_ramp=False):
    require(type(users) is int and users in (250, 500), 'UNSUPPORTED_USER_COUNT')
    require(not slower_ramp or users == 500, 'UNSUPPORTED_RAMP')
    files = 'browse-scaling-' + run_id
    base.kube('apply', '-f', str(ROOT / 'kubernetes/checks/read-load-serviceaccount.yaml'))
    cm = base.kube('create', 'configmap', files, '-n', 'trafficxx',
        '--from-file=probe.py=' + str(ROOT / 'scripts/check-browse-autoscaling-load.py'),
        '--from-file=read_probe.py=' + str(ROOT / 'scripts/check-read-load.py'), '--dry-run=client', '-o', 'json')
    base.kube('create', '-f', '-', data=cm)
    pods = json.loads(base.kube('get', 'pods', '-n', 'trafficxx', '-l',
        'app.kubernetes.io/name=trafficxx-browse', '-o', 'json'))['items']
    node = next(p['spec']['nodeName'] for p in pods if not p['metadata'].get('deletionTimestamp'))
    patch = [{'op': 'replace', 'path': '/metadata/name', 'value': job_name},
             {'op': 'add', 'path': '/metadata/labels/trafficxx.io~1run-id', 'value': run_id},
             {'op': 'remove', 'path': '/spec/ttlSecondsAfterFinished'},
             {'op': 'replace', 'path': '/spec/activeDeadlineSeconds', 'value': 450},
             {'op': 'replace', 'path': '/spec/template/spec/nodeName', 'value': node},
             {'op': 'replace', 'path': '/spec/template/spec/volumes/0/configMap/name', 'value': files},
             {'op': 'replace', 'path': '/spec/template/spec/containers/0/image',
              'value': app['spec']['template']['spec']['containers'][0]['image']},
             {'op': 'add', 'path': '/spec/template/spec/containers/0/args',
              'value': [run_id, str(users)] + (['120'] if slower_ramp else [])}]
    manifest = base.kube('patch', '--local', '-f', str(ROOT / 'kubernetes/checks/browse-users.yaml'),
        '--type=json', '--patch', json.dumps(patch), '-o', 'json')
    try:
        return json.loads(base.kube('create', '-f', '-', '-o', 'json', data=manifest))
    except subprocess.SubprocessError:
        # A lost create response is not proof that no workload was started.
        candidate = json.loads(base.kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
        require(candidate['metadata'].get('labels', {}).get('trafficxx.io/run-id') == run_id,
                'AMBIGUOUS_JOB_OWNERSHIP')
        return candidate


def idle_two(uid, query):
    app = deployment(uid)
    require(app['spec']['replicas'] == 2 and app.get('status', {}).get('readyReplicas') == 2,
            'WAITING_FOR_NATURAL_DOWNSCALE')
    return check(query)


def check_pool_errors(query):
    for field in ('borrow_errors_total', 'connection_errors_total', 'queue_full_total',
                  'acquire_timeouts_total', 'closed_rejections_total', 'bad_returns_total'):
        require(scalar(query, 'sum(increase(' + M + field + F + '[1m]))') == 0,
                'POOL_ERROR_COUNTER_INCREASE')


def wait_idle():
    """Observe recovery only; never create a Job or change replica counts."""
    trial.context()
    trial.no_active_load()
    with prometheus() as query:
        app = wait_baseline(query)
        emit('BROWSE_WAITING_FOR_IDLE_BASELINE', current_replicas=app['spec']['replicas'],
             timeout_seconds=600, note='Read-only recovery check; no new load or manual scaling.')
        wait_until(lambda: idle_two(app['metadata']['uid'], query), 600)
    trial.health()
    database_pressure()
    emit('BROWSE_AUTOSCALING_RECOVERY_OK', ready_replicas=2, pool_per_pod=8, inventory_writes=False,
         note='Post-run recovery only; not a replacement for the original continuous resource audit.')


def diagnostic_window(job):
    """Use this Job's timestamps, never an unaligned recent traffic window."""
    conditions = job.get('status', {}).get('conditions', [])
    terminal = [r for r in conditions if r.get('status') == 'True' and r.get('type') in ('Complete', 'Failed')]
    require(len(terminal) == 1, 'DIAGNOSTIC_JOB_NOT_TERMINAL')
    start = datetime.fromisoformat(job['status']['startTime'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(terminal[0]['lastTransitionTime'].replace('Z', '+00:00'))
    require(start.tzinfo is not None and end.tzinfo is not None and 0 <= (end - start).total_seconds() <= 900,
            'UNEXPECTED_DIAGNOSTIC_WINDOW')
    return start, end, max(60, math.ceil((end - start).total_seconds()) + 60)


def optional_scalar(query, expression):
    rows = query(expression)
    if not rows:
        return None
    require(len(rows) == 1, 'UNEXPECTED_DIAGNOSTIC_SERIES')
    value = float(rows[0]['value'][1])
    require(math.isfinite(value) and value >= 0, 'INVALID_DIAGNOSTIC_SAMPLE')
    return round(value, 6)


def post_job_coverage(before, after, terminal_timestamp):
    initial = {row['metric']['instance'] for row in before}
    stamps = {}
    for row in after:
        instance, stamp = row['metric']['instance'], float(row['value'][1])
        require(instance not in stamps and math.isfinite(stamp) and stamp >= 0,
                'INVALID_DIAGNOSTIC_COVERAGE')
        stamps[instance] = stamp
    return {'instances_at_terminal': len(initial), 'instances_in_post_window': len(stamps),
            'all_original_instances_sampled_after_terminal':
                all(name in stamps and stamps[name] > terminal_timestamp for name in initial) if initial else None}


def distribution_expressions(window, when, counter_window, counter_when):
    """Keep per-instance counter tails and pre-terminal peaks separate."""
    rows = {field + '_increase': 'increase(' + M + field + F + '[' + str(counter_window) +
            's] @ ' + counter_when + ')' for field in (
                'queue_full_total', 'acquire_timeouts_total', 'closed_rejections_total',
                'borrow_errors_total', 'connection_errors_total', 'connections_lost_total',
                'bad_returns_total', 'borrows_total')}
    for label, expression in (
            ('sampled_peak_waiting', M + 'waiting' + F),
            ('sampled_peak_busy_or_opening', 'clamp_min(' + M + 'size' + F + ' - ' + M + 'available' + F + ',0)'),
            ('sampled_peak_borrow_rate', 'rate(' + M + 'borrows_total' + F + '[1m])')):
        rows[label] = 'max_over_time((' + expression + ')[' + str(window) + 's:15s] @ ' + when + ')'
    rows['scrape_samples_before_terminal'] = 'count_over_time(up' + F + '[' + str(window) + 's] @ ' + when + ')'
    rows['last_up_source_timestamp_at_terminal'] = 'timestamp(up' + F + ' @ ' + when + ')'
    rows['last_borrow_source_timestamp_after_terminal'] = 'timestamp(' + M + 'borrows_total' + F + ' @ ' + counter_when + ')'
    return rows


def instance_distribution(query, expressions):
    fields, instances = {}, set()
    for field, expression in expressions.items():
        values = {}
        for row in query(expression):
            instance = row.get('metric', {}).get('instance', '')
            value = float(row['value'][1])
            require(re.fullmatch(r'[A-Za-z0-9.\[\]:-]{1,150}', instance) is not None
                    and instance not in values, 'INVALID_DIAGNOSTIC_INSTANCE')
            require(math.isfinite(value) and value >= 0, 'INVALID_DIAGNOSTIC_SAMPLE')
            values[instance] = round(value, 6)
        fields[field] = values
        instances.update(values)
    # Missing series are null, never a claim of zero traffic or zero errors.
    return {instance: {field: values.get(instance) for field, values in fields.items()}
            for instance in sorted(instances)}


def waiting_history(query, end):
    """Actual stored samples, not subquery resampling or observer wall-clock reads."""
    rows = query(M + 'waiting' + F + '[90s] @ ' + str(int(end.timestamp())))
    require(len(rows) <= 8, 'UNEXPECTED_WAITING_HISTORY_INSTANCES')
    result = {}
    for row in rows:
        instance = row.get('metric', {}).get('instance', '')
        require(re.fullmatch(r'[A-Za-z0-9.\[\]:-]{1,150}', instance) is not None
                and instance not in result, 'INVALID_DIAGNOSTIC_INSTANCE')
        samples = row.get('values', [])
        require(len(samples) <= 32, 'UNEXPECTED_WAITING_HISTORY_SAMPLES')
        values, previous = [], -math.inf
        for stamp, value in samples:
            stamp, value = float(stamp), float(value)
            require(math.isfinite(stamp) and previous < stamp and end.timestamp() - 90 < stamp <= end.timestamp()
                    and math.isfinite(value) and value >= 0, 'INVALID_WAITING_HISTORY_SAMPLE')
            previous = stamp
            values.append({'source_timestamp_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                           'waiting': value})
        result[instance] = values
    return result


def diagnostic_outcome(job, logs, *, allow_interrupted=False):
    try:
        outcome = json.loads(logs.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        outcome = {}
    run_id = job['metadata'].get('labels', {}).get('trafficxx.io/run-id')
    require(isinstance(run_id, str) and re.fullmatch(r'[0-9a-f]{12}', run_id), 'DIAGNOSTIC_RUN_MISMATCH')
    if isinstance(outcome, dict) and outcome.get('result') in ('BROWSE_SCALING_LOAD_FAILED', 'BROWSE_SCALING_LOAD_OK'):
        require(outcome.get('users') == 500 and outcome.get('run_id') == run_id, 'DIAGNOSTIC_RUN_MISMATCH')
        return outcome
    # Our stop shortens the deadline while retaining logs. Admit missing final
    # summaries only for the fixed, deadline-stopped step30 Job with exact args.
    require(allow_interrupted and job['metadata'].get('name') in (
                'browse-users-500-autoscaling-step30', 'browse-users-500-autoscaling-sourcewait')
            and any(c.get('type') == 'Failed' and c.get('status') == 'True' and c.get('reason') == 'DeadlineExceeded'
                    for c in job.get('status', {}).get('conditions', [])), 'DIAGNOSTIC_RUN_MISMATCH')
    containers = job.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
    require(len(containers) == 1 and containers[0].get('name') == 'check'
            and containers[0].get('args') == [run_id, '500', '120'], 'INTERRUPTED_JOB_ARGUMENTS_MISMATCH')
    return {'result': 'BROWSE_SCALING_WORKLOAD_INTERRUPTED', 'run_id': run_id,
            'users': 500, 'start_ramp_seconds': 120, 'final_workload_summary_available': False,
            'note': 'Deadline-stopped Job without a final summary. HTTP counts, errors and participation are unknown.'}


def diagnose_500(*, slower_ramp=False, earlier_scaling=False, verify_early=False, distribution=False,
                 faster_scale_up=False, verify_waiting_samples=False):
    """Read-only historical evidence; no load, configuration or SQL mutation."""
    trial.context()
    trial.no_active_load()
    name = ('browse-users-500-autoscaling-sourcewait' if verify_waiting_samples else
            'browse-users-500-autoscaling-step30' if faster_scale_up else
            'browse-users-500-autoscaling-pressure3-v2' if verify_early else
            'browse-users-500-autoscaling-pressure3' if earlier_scaling else
            'browse-users-500-autoscaling-ramp120' if slower_ramp else 'browse-users-500-autoscaling')
    job = json.loads(base.kube('get', 'job', name, '-n', 'trafficxx', '-o', 'json'))
    start, end, window = diagnostic_window(job)
    log_capture = {'available': True}
    try:
        logs = base.kube('logs', '-n', 'trafficxx', 'job/' + name, '-c', 'check')
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        if not (faster_scale_up or verify_waiting_samples):
            raise
        # Metadata must independently prove this exact interrupted stage.
        # Missing logs never permit a completed or unrecognized Job to pass.
        outcome = diagnostic_outcome(job, '', allow_interrupted=True)
        log_capture = {'available': False, **failure_details(error)}
    else:
        outcome = diagnostic_outcome(job, logs, allow_interrupted=faster_scale_up or verify_waiting_samples)
    when = str(int(end.timestamp()))
    require(not (slower_ramp or earlier_scaling or verify_early or faster_scale_up or verify_waiting_samples)
            or outcome.get('start_ramp_seconds') == 120,
            'DIAGNOSTIC_RAMP_MISMATCH')
    # A final rejection may not be exported until the next scrape, after the
    # diagnostic Job has already become Failed. Retain peak-window alignment,
    # but give cumulative counters four scrape intervals to capture that tail.
    counter_end = end + timedelta(seconds=60)
    counter_when, counter_window = str(int(counter_end.timestamp())), window + 60
    with prometheus() as query:
        extra = {}
        counters = {}
        for field in ('queue_full_total', 'acquire_timeouts_total', 'closed_rejections_total',
                      'borrow_errors_total', 'connection_errors_total', 'connections_lost_total', 'bad_returns_total'):
            counters[field] = optional_scalar(query, 'sum(increase(' + M + field + F +
                '[' + str(counter_window) + 's] @ ' + counter_when + '))')
        peaks = {}
        for label, expression in (
                ('healthy_targets', 'sum(up' + F + ')'),
                ('waiting', 'sum(' + M + 'waiting' + F + ')'),
                ('busy_or_opening', 'sum(clamp_min(' + M + 'size' + F + ' - ' + M + 'available' + F + ',0))'),
                ('borrow_rate', 'sum(rate(' + M + 'borrows_total' + F + '[1m]))')):
            peaks[label] = optional_scalar(query, 'max_over_time((' + expression + ')[' +
                str(window) + 's:15s] @ ' + when + ')')
        coverage = post_job_coverage(query(M + 'borrows_total' + F + ' @ ' + when),
            query('timestamp(' + M + 'borrows_total' + F + ' @ ' + counter_when + ')'), end.timestamp())
        if distribution:
            extra['per_instance'] = instance_distribution(query,
                distribution_expressions(window, when, counter_window, counter_when))
            extra['distribution_note'] = ('Instances are Prometheus scrape addresses. Borrow increases include readiness '
                'and other traffic, not just workload HTTP requests. Counter tails include 60 seconds after terminal; '
                'peaks and scrape counts end at terminal. New/missing series are null; sampled peaks can miss bursts. '
                'Current configuration is not historical endpoint or routing evidence.')
            extra['waiting_samples_before_terminal'] = waiting_history(query, end)
            extra['waiting_history_note'] = ('Actual stored source samples from the final 90 seconds before Job terminal. '
                'Not observer poll timestamps or proof of continuously queued requests between scrapes. '
                'Samples after the stop request may include recovery; missing history is not zero waiting.')
    current = json.loads(base.kube('get', 'job', name, '-n', 'trafficxx', '-o', 'json'))
    require(current['metadata']['uid'] == job['metadata']['uid'], 'JOB_REPLACED')
    emit('BROWSE_500_DIAGNOSTICS_OK', job=name, run_id=outcome['run_id'],
         job_start_utc=start.isoformat(), job_terminal_utc=end.isoformat(),
         query_window_seconds=window, counter_increases=counters, sampled_peaks=peaks,
         counter_query_end_utc=counter_end.isoformat(), counter_query_window_seconds=counter_window,
         post_terminal_observation_seconds=60, post_job_sample_coverage=coverage,
         workload=outcome, workload_log_capture=log_capture, inventory_writes=False, **extra,
         note='Read-only capture, not a load pass. Peaks end at Job termination; counters include 60 seconds '
              'after termination to capture the next scrapes. Both include warmup and 60 seconds before start. '
              'Check post-job coverage before interpreting zero counters. Increases are extrapolated estimates; '
              'peaks are sampled and missing data is null.')


def validate_load(outcome, run_id, users, released, *, slower_ramp=False):
    require(outcome.get('result') == 'BROWSE_SCALING_LOAD_OK' and outcome.get('run_id') == run_id,
            'LOAD_EVIDENCE_MISSING')
    require(released and outcome.get('users_with_requests') == users and
            180 <= outcome.get('elapsed_seconds', 0) <= 195 and
            type(outcome.get('attempts')) is int and 1 <= outcome['attempts'] <= users * 90 and
            not outcome.get('errors') and outcome.get('status_counts') == {'200': outcome['attempts']},
            'INCOMPLETE_LOAD_EVIDENCE')
    if users == 500:
        require(outcome.get('users') == 500 and outcome.get('scheduled_duration_seconds') == 180 and
                outcome.get('start_ramp_seconds') == (120 if slower_ramp else 60) and outcome.get('think_seconds') == 2 and
                outcome.get('max_workload_attempts') == 45000, 'HIGHER_STAGE_PACING_NOT_VERIFIED')


def drill(*, retry_metric_gap=False, verification_run=False, users=250, slower_ramp=False, earlier_scaling=False,
          verify_early=False, faster_scale_up=False, verify_waiting_samples=False):
    require(type(users) is int and users in (250, 500), 'UNSUPPORTED_USER_COUNT')
    require(not (retry_metric_gap and verification_run), 'CONFLICTING_DRILL_MODES')
    require(users == 250 or not (retry_metric_gap or verification_run), 'CONFLICTING_DRILL_MODES')
    require(not slower_ramp or users == 500, 'UNSUPPORTED_RAMP')
    require(not earlier_scaling or (users == 500 and not slower_ramp), 'CONFLICTING_DRILL_MODES')
    require(not verify_early or (users == 500 and not any((retry_metric_gap, verification_run,
                                                       slower_ramp, earlier_scaling))), 'CONFLICTING_DRILL_MODES')
    require(not faster_scale_up or (users == 500 and not any((retry_metric_gap, verification_run,
                            slower_ramp, earlier_scaling, verify_early))), 'CONFLICTING_DRILL_MODES')
    require(not verify_waiting_samples or (users == 500 and not any((retry_metric_gap, verification_run,
                    slower_ramp, earlier_scaling, verify_early, faster_scale_up))), 'CONFLICTING_DRILL_MODES')
    faster_scale_up = faster_scale_up or verify_waiting_samples
    earlier_scaling = earlier_scaling or verify_early or faster_scale_up
    pressure_target = '3' if earlier_scaling else '5'
    scale_up_seconds = 30 if faster_scale_up else 60
    slower_ramp = slower_ramp or earlier_scaling
    job_name = ('browse-users-500-autoscaling-sourcewait' if verify_waiting_samples else
                'browse-users-500-autoscaling-step30' if faster_scale_up else
                'browse-users-500-autoscaling-pressure3-v2' if verify_early else
                'browse-users-500-autoscaling-pressure3' if earlier_scaling else
                ('browse-users-500-autoscaling-ramp120' if slower_ramp else 'browse-users-500-autoscaling') if users == 500 else
                JOB + ('-v3' if verification_run else '-v2' if retry_metric_gap else ''))
    trial.context()
    trial.no_active_load()
    require(not base.kube('get', 'job', job_name, '-n', 'trafficxx', '--ignore-not-found', '-o', 'name'),
            'INSPECT_EXISTING_DRILL_JOB')
    trial.health()
    database_pressure()
    job = None
    start = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with prometheus() as query:
        app = wait_baseline(query)
        if (retry_metric_gap or verification_run or users == 500) and app['spec']['replicas'] != 2:
            emit('BROWSE_WAITING_FOR_IDLE_BASELINE', current_replicas=app['spec']['replicas'],
                 timeout_seconds=600, note='Read-only wait for natural HPA downscale; no load is started.')
            app = wait_until(lambda: idle_two(app['metadata']['uid'], query), 600)
        require(app['spec']['replicas'] == 2, 'START_WITH_TWO_REPLICAS')
        owned_hpa(pressure_target=pressure_target, scale_up_seconds=scale_up_seconds)
        start = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        original = trial.fingerprint(app['spec']['template'])
        run_id = uuid4().hex[:12]
        peak, released, last_database = 2, False, time.monotonic()
        samples, first_seen, resource_watch = {}, {}, trial.users.ResourceWatch()
        waiting_watch = PoolWaitingWatch()
        metric_readiness = MetricReadiness()
        stage, primary_error, job_terminal = 'create_load_job', None, False
        try:
            job = (create_job(app, run_id, job_name, users=500, slower_ramp=True) if slower_ramp else
                   create_job(app, run_id, job_name, users=500) if users == 500 else
                   create_job(app, run_id, job_name))
            emit('BROWSE_AUTOSCALING_DRILL_STARTED', run_id=run_id, job=job_name, users=users, seconds=180,
                 pressure_target_per_pod=int(pressure_target),
                 scale_up_period_seconds=scale_up_seconds,
                 start_ramp_seconds=120 if slower_ramp else 60 if users == 500 else 2, max_workload_attempts=users * 90,
                 note='Real Browse GETs; no manual scale commands or inventory writes.')
            deadline = time.monotonic() + 480
            while True:
                stage = 'observe_deployment'
                current = deployment(app['metadata']['uid'])
                require(trial.fingerprint(current['spec']['template']) == original, 'DEPLOYMENT_TEMPLATE_CHANGED')
                ready = current.get('status', {}).get('readyReplicas', 0)
                peak = max(peak, ready)
                emit('BROWSE_SCALING_OBSERVATION', desired=current['spec']['replicas'], ready=ready,
                     resource_gate_released=released)
                stage = 'observe_autoscaler'
                metric_readiness.observe(owned_hpa(require_active=False, pressure_target=pressure_target,
                                                 scale_up_seconds=scale_up_seconds), time.monotonic())
                stage = 'observe_load_job'
                state = json.loads(base.kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
                require(state['metadata']['uid'] == job['metadata']['uid'], 'JOB_REPLACED')
                terminal = {r['type'] for r in state.get('status', {}).get('conditions', []) if r['status'] == 'True'}
                job_terminal = bool(terminal & {'Complete', 'Failed'})
                if 'Failed' in terminal:
                    stage = 'read_failed_load_logs'
                    print(base.kube('logs', '-n', 'trafficxx', 'job/' + job_name, '-c', 'check'), flush=True)
                require('Failed' not in terminal, 'LOAD_JOB_FAILED')
                # A completed generator no longer has live container usage.
                # Validate its final workload evidence below, rather than
                # interpreting disappearing completed-pod metrics as a fault.
                if 'Complete' in terminal:
                    break
                # Source-timestamp freshness and sustained pressure use the same
                # limits as earlier trials. New pods have bounded metrics warmup.
                stage = 'read_resource_metrics'
                payload = trial.users.metrics()
                stage = 'read_observed_pods'
                pods = json.loads(base.kube('get', 'pods', '-n', 'trafficxx', '-o', 'json'))['items']
                observed = [p for p in pods if not p['metadata'].get('deletionTimestamp') and
                            (p['metadata'].get('labels', {}).get('app.kubernetes.io/name') == 'trafficxx-browse'
                             or p['metadata'].get('labels', {}).get('job-name') == job_name)]
                require(len([p for p in observed if p['metadata'].get('labels', {}).get('app.kubernetes.io/name')
                             == 'trafficxx-browse']) <= 4, 'BROWSE_POD_BUDGET_EXCEEDED')
                expected = {}
                generator = None
                for pod in observed:
                    name = pod['metadata']['name']
                    is_generator = pod['metadata'].get('labels', {}).get('job-name') == job_name
                    if is_generator:
                        require(pod.get('status', {}).get('phase') != 'Failed', 'GENERATOR_FAILED')
                        if pod.get('status', {}).get('phase') == 'Succeeded':
                            # Pod phase can precede the Job Complete condition.
                            continue
                        generator = name
                    else:
                        require(all(s.get('restartCount', 0) == 0 for s in pod.get('status', {}).get('containerStatuses', [])),
                                'BROWSE_PROCESS_RESTARTED')
                    expected[name] = {'container': 'check' if is_generator else 'browse',
                                      'cpu': 1 if is_generator else .5, 'memory': 256}
                    first_seen.setdefault(name, time.monotonic())
                stage = 'validate_resource_samples'
                values = {}
                for name, limits in expected.items():
                    try:
                        values.update(trial.users.sample(payload, {name: limits}))
                    except trial.users.MetricSampleError:
                        require(not samples.get(name) and time.monotonic() - first_seen[name] < 210,
                                'RESOURCE_METRICS_STALE')
                resource_watch.add(values, time.monotonic())
                for name in expected:
                    if name not in values:
                        if time.monotonic() - first_seen[name] >= 210:
                            emit('BROWSE_RESOURCE_METRIC_MISSING', pod=name,
                                 note='Expected a live container sample; completed generators are excluded.')
                        require(time.monotonic() - first_seen[name] < 210, 'RESOURCE_METRICS_MISSING')
                    else:
                        samples.setdefault(name, set()).add(values[name]['timestamp'])
                if not released and generator and all(len(samples.get(name, set())) >= 2 for name in expected):
                    stage = 'release_load_gate'
                    trial.users.release_gate(generator, run_id, job_name)
                    released = True
                    waiting_watch.not_before = time.time()
                    emit('BROWSE_SCALING_RESOURCE_GATE_RELEASED', run_id=run_id)
                if released:
                    stage = 'observe_pool_pressure'
                    waiting_watch.observe(query(M + 'waiting' + F + '[45s]'), time.time())
                    check_pool_errors(query)
                if time.monotonic() - last_database >= 60:
                    stage = 'observe_database_metrics'
                    database_pressure()
                    last_database = time.monotonic()
                require(time.monotonic() < deadline, 'LOAD_JOB_TIMEOUT')
                time.sleep(5)
            stage = 'validate_completed_workload'
            logs = base.kube('logs', '-n', 'trafficxx', 'job/' + job_name, '-c', 'check')
            print(logs, flush=True)
            outcome = json.loads(logs.splitlines()[-1])
            validate_load(outcome, run_id, users, released, slower_ramp=slower_ramp)
            require(peak >= 3, 'NATURAL_SCALE_UP_NOT_OBSERVED')
            check_pool_errors(query)
            gaps = metric_readiness.summary()
            emit('BROWSE_SCALE_UP_OBSERVED', peak_ready_replicas=peak)
            # No manual replica restoration: success requires the actual controller to downscale.
            stage = 'wait_natural_recovery'
            wait_until(lambda: idle_two(app['metadata']['uid'], query), 600)
            stage = 'verify_final_health'
            trial.health()
            stage = 'verify_final_database_metrics'
            database_pressure()
            require(trial.database_metrics(start), 'DATABASE_METRIC_SAMPLES_UNAVAILABLE')
            emit('BROWSE_AUTOSCALING_DRILL_OK', run_id=run_id, job=job_name, users=users, ready_replicas_before=2,
                 pressure_target_per_pod=int(pressure_target),
                 scale_up_period_seconds=scale_up_seconds,
                 start_ramp_seconds=120 if slower_ramp else 60 if users == 500 else 2,
                 peak_ready_replicas=peak, ready_replicas_after=2, inventory_writes=False,
                 resources=resource_watch.summary(),
                 pool_waiting_observations=waiting_watch.summary(),
                 hpa_metric_observations=gaps,
                 latency_slo_tested=False, notification_delivery_configured=False)
        except (Exception, KeyboardInterrupt) as error:
            primary_error = error
            error.observer_context = {'stage': stage, 'job': job_name, 'run_id': run_id,
                                      'resource_gate_released': released, 'resources': resource_watch.summary(),
                                      'pool_waiting_observations': waiting_watch.summary()}
            raise
        finally:
            if job is not None:
                try:
                    cleanup = cleanup_load(job, job_name, terminal=job_terminal)
                    if primary_error is not None:
                        primary_error.observer_context['load_cleanup'] = cleanup
                except (Exception, KeyboardInterrupt) as cleanup_error:
                    emit('BROWSE_AUTOSCALING_CLEANUP_FAILED', job=job_name, **failure_details(cleanup_error),
                         note='Job may still run until its deadline. Keep the Job and inspect its status.')
                    if primary_error is None:
                        raise
                    primary_error.observer_context['load_cleanup'] = 'failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('deploy', 'check', 'drill', 'wait-idle', 'diagnose-500', 'diagnose-500-slow',
                                         'configure-early', 'diagnose-500-early', 'diagnose-500-verify',
                                         'diagnose-500-distribution', 'configure-fast-scale-up', 'diagnose-500-step30',
                                         'diagnose-500-sourcewait'))
    parser.add_argument('--users', type=int, choices=(250, 500), default=250,
                        help='Only the explicitly selected stage; 500 uses a 60-second arrival ramp.')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--retry-metric-gap', action='store_true',
                        help='Use a distinct v2 Job, preserving the original stopped drill.')
    modes.add_argument('--verification-run', action='store_true',
                       help='Use a distinct v3 Job to verify the corrected observer end to end.')
    modes.add_argument('--slower-ramp', action='store_true',
                       help='500 users only: compare a 120-second arrival ramp in a separate Job.')
    modes.add_argument('--earlier-scaling', action='store_true',
                       help='500 users only: require pressure target 3, retain the 120-second ramp.')
    modes.add_argument('--verify-early', action='store_true',
                       help='Repeat the same 500-user pressure-3 trial in a distinct v2 Job with observer diagnostics.')
    modes.add_argument('--faster-scale-up', action='store_true',
                       help='500 users only: compare one added pod per 30 seconds, same pressure-3 profile and 120-second ramp.')
    modes.add_argument('--verify-waiting-samples', action='store_true',
                       help='Repeat the 500-user step30 profile with the corrected per-instance source-sample guard.')
    args = parser.parse_args()
    action = args.action
    if any((args.retry_metric_gap, args.verification_run, args.slower_ramp, args.earlier_scaling,
            args.verify_early, args.faster_scale_up, args.verify_waiting_samples)) and action != 'drill':
        parser.error('drill modes require the drill action')
    if args.users != 250 and (action != 'drill' or args.retry_metric_gap or args.verification_run):
        parser.error('--users 500 requires drill without a 250-user retry mode')
    if args.slower_ramp and args.users != 500:
        parser.error('--slower-ramp requires --users 500')
    if args.earlier_scaling and args.users != 500:
        parser.error('--earlier-scaling requires --users 500')
    if args.verify_early and args.users != 500:
        parser.error('--verify-early requires --users 500')
    if args.faster_scale_up and args.users != 500:
        parser.error('--faster-scale-up requires --users 500')
    if args.verify_waiting_samples and args.users != 500:
        parser.error('--verify-waiting-samples requires --users 500')
    try:
        if action == 'deploy':
            deploy()
        elif action == 'drill':
            drill(retry_metric_gap=args.retry_metric_gap, verification_run=args.verification_run,
                  users=args.users, slower_ramp=args.slower_ramp, earlier_scaling=args.earlier_scaling,
                  verify_early=args.verify_early, faster_scale_up=args.faster_scale_up,
                  verify_waiting_samples=args.verify_waiting_samples)
        elif action == 'configure-early':
            configure_early()
        elif action == 'configure-fast-scale-up':
            configure_fast_scale_up()
        elif action == 'wait-idle':
            wait_idle()
        elif action == 'diagnose-500':
            diagnose_500()
        elif action == 'diagnose-500-slow':
            diagnose_500(slower_ramp=True)
        elif action == 'diagnose-500-early':
            diagnose_500(earlier_scaling=True)
        elif action == 'diagnose-500-verify':
            diagnose_500(verify_early=True)
        elif action == 'diagnose-500-distribution':
            diagnose_500(verify_early=True, distribution=True)
        elif action == 'diagnose-500-step30':
            diagnose_500(faster_scale_up=True, distribution=True)
        elif action == 'diagnose-500-sourcewait':
            diagnose_500(verify_waiting_samples=True, distribution=True)
        else:
            trial.context()
            with prometheus() as query:
                wait_baseline(query)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        emit('BROWSE_AUTOSCALING_FAILED', action=action, **failure_details(error),
             observer=getattr(error, 'observer_context', None),
             note='Inspect the preserved Job/HPA/ScaledObject. No manual scale-down or SQL reset is performed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
