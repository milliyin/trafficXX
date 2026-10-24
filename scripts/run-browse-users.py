"""Cloud Shell: run one explicitly selected 50- or 100-user stage, observing Kubernetes resource usage."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import importlib.util
import json
import math
from pathlib import Path
import re
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
JOB = 'browse-users-50-v3'
# Reviewed for the reported collector with no metric-resolution override.
RESOURCE_MAX_AGE = 90
GENERATOR_METRICS_WARMUP = 210


class MetricSampleError(ValueError):
    def __init__(self, pod, timestamp, age):
        super().__init__('POD_METRICS_NOT_FRESH')
        self.details = {'pod': pod, 'source_timestamp': timestamp, 'age_seconds': round(age, 3),
                        'max_age_seconds': RESOURCE_MAX_AGE}


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load('run-read-load')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def quantity(value, kind):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)', value)
    require(match is not None, 'INVALID_RESOURCE_QUANTITY')
    factors = ({'': 1, 'n': 1e-9, 'u': 1e-6, 'm': 1e-3} if kind == 'cpu'
               else {'': 1 / 1048576, 'Ki': 1 / 1024, 'Mi': 1, 'Gi': 1024})
    require(match[2] in factors, 'UNSUPPORTED_RESOURCE_UNIT')
    result = float(Decimal(match[1])) * factors[match[2]]
    require(math.isfinite(result) and result >= 0, 'INVALID_RESOURCE_VALUE')
    return result


def sample(payload, expected, now=None):
    now = time.time() if now is None else now
    result = {}
    for row in payload['items']:
        name = row['metadata']['name']
        if name not in expected:
            continue
        require(name not in result, 'DUPLICATE_POD_METRICS')
        timestamp = row['timestamp'].replace('Z', '+00:00')
        timestamp = re.sub(r'(\.\d{6})\d+(?=[+-]|$)', r'\1', timestamp)
        stamp = datetime.fromisoformat(timestamp)
        require(stamp.tzinfo is not None, 'POD_METRICS_TIMEZONE_MISSING')
        age = now - stamp.timestamp()
        if not -5 <= age <= RESOURCE_MAX_AGE:
            raise MetricSampleError(name, row['timestamp'], age)
        containers = row['containers']
        require(len(containers) == 1 and containers[0]['name'] == expected[name]['container'],
                'UNEXPECTED_METRIC_CONTAINERS')
        cpu = quantity(containers[0]['usage']['cpu'], 'cpu')
        memory = quantity(containers[0]['usage']['memory'], 'memory')
        result[name] = {'cpu_cores': cpu, 'memory_mib': memory, 'timestamp': row['timestamp'],
                        'cpu_limit_fraction': cpu / expected[name]['cpu'],
                        'memory_limit_fraction': memory / expected[name]['memory']}
    return result


class ResourceWatch:
    def __init__(self):
        self.rows, self.high_since = {}, {}

    def add(self, values, now):
        for name, value in values.items():
            row = self.rows.setdefault(name, {'timestamps': set(), 'max_cpu_cores': 0,
                                             'max_memory_mib': 0})
            row['timestamps'].add(value['timestamp'])
            row['max_cpu_cores'] = max(row['max_cpu_cores'], value['cpu_cores'])
            row['max_memory_mib'] = max(row['max_memory_mib'], value['memory_mib'])
            for metric, limit, hold in (('cpu_limit_fraction', .8, 20),
                                        ('memory_limit_fraction', .85, 10)):
                key = name, metric
                if value[metric] >= limit:
                    self.high_since.setdefault(key, now)
                    require(now - self.high_since[key] < hold, 'RESOURCE_STOP_' + metric.upper())
                else:
                    self.high_since.pop(key, None)

    def summary(self):
        return {name: {'distinct_samples': len(row['timestamps']),
                       'sampled_max_cpu_cores': round(row['max_cpu_cores'], 6),
                       'sampled_max_memory_mib': round(row['max_memory_mib'], 3)}
                for name, row in self.rows.items()}


def metrics():
    return json.loads(base.kube('get', '--raw', '/apis/metrics.k8s.io/v1beta1/namespaces/trafficxx/pods'))


def validate(outcome, run_id, users=50):
    require(type(users) is int and users in (50, 100), 'INVALID_STAGE')
    require(outcome.get('result') == 'BROWSE_USERS_WORKLOAD_OK' and outcome.get('run_id') == run_id
            and outcome.get('users') == users and outcome.get('users_with_requests') == users
            and type(outcome.get('attempts')) is int and users <= outcome['attempts'] <= users * 30
            and outcome.get('valid_responses') == outcome['attempts']
            and outcome.get('status_counts') == {'200': outcome['attempts']}
            and not outcome.get('errors') and not outcome.get('observer_checks')
            and outcome.get('inventory_writes') is False
            and outcome.get('availability_before_and_after') == 100
            and outcome.get('pool_processes_unchanged') is True
            and outcome.get('latency_slo_tested') is False
            and 60 <= outcome.get('elapsed_seconds', 0) <= 75
            and 1 <= outcome.get('sampled_peak_workload_http_in_flight', 0) <= users,
            'INCOMPLETE_WORKLOAD_EVIDENCE')
    values = [outcome['latency_ms'][name] for name in ('p50', 'p95', 'p99', 'max')]
    require(all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                for value in values) and values == sorted(values), 'INVALID_LATENCY_SUMMARY')
    pools = outcome.get('pools', [])
    require(len(pools) == 2 and len({row.get('pod') for row in pools}) == 2
            and all(row.get('pool_max') == 4 and row.get('pool_error_counters_unchanged') is True
                    and 1 <= row.get('sampled_peak_size', 0) <= 4
                    and 0 <= row.get('sampled_peak_waiting', -1) <= 8
                    and row.get('borrows_delta', -1) >= 0
                    and row.get('connection_attempts_delta', -1) >= 0 for row in pools),
            'POOL_OBSERVATIONS_INCOMPLETE')


def stop_owned_job(uid, job_name=JOB):
    # Shorten only this diagnostic Job's deadline, retaining Pod logs and CM.
    base.kube('patch', 'job', job_name, '-n', 'trafficxx', '--type=json', '--patch', json.dumps([
        {'op': 'test', 'path': '/metadata/uid', 'value': uid},
        {'op': 'replace', 'path': '/spec/activeDeadlineSeconds', 'value': 1}]))


def release_gate(pod, run_id, job_name=JOB):
    require(pod.startswith(job_name + '-') and re.fullmatch(r'[0-9a-f]{12}', run_id), 'UNEXPECTED_GATE_TARGET')
    base.kube('exec', '-n', 'trafficxx', 'pod/' + pod, '-c', 'check', '--', 'python', '-c',
              "from pathlib import Path; p=Path('/state/start.tmp'); p.write_text("
              + json.dumps(run_id) + "); p.replace('/state/start')")


def execute(*, preflight_only=False, users=50, diagnostic_retry=False):
    require(type(users) is int and users in (50, 100), 'INVALID_STAGE')
    require(not diagnostic_retry or users == 100, 'DIAGNOSTIC_RETRY_REQUIRES_100_USERS')
    job_name = ('browse-users-100-diagnostic' if diagnostic_retry else
                JOB if users == 50 else 'browse-users-100')
    stage, job, files_name = 'subscription', None, None
    create_requested = False
    gate_released = False
    watch = ResourceWatch()
    logs = ''
    try:
        require(base.command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'])
                == '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85', 'WRONG_SUBSCRIPTION')
        stage = 'kubernetes_context'
        require(base.kube('config', 'current-context') == 'trafficxx-lab-aks', 'WRONG_CONTEXT')
        stage = 'existing_jobs'
        require(not base.kube('get', 'job', job_name, '-n', 'trafficxx', '--ignore-not-found', '-o', 'name'),
                'INSPECT_EXISTING_USER_LOAD_JOB')
        other = json.loads(base.kube('get', 'jobs', '-n', 'trafficxx', '-o', 'json'))['items']
        require(not any((row['metadata']['name'] == 'read-load-baseline'
                         or row['metadata']['name'].startswith('browse-users-'))
                        and not any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                                    for c in row.get('status', {}).get('conditions', []))
                        for row in other), 'OTHER_READ_LOAD_ACTIVE')
        stage = 'serving_pods'
        flow = load('check-browse-concurrency')
        before = {api: base.snapshot(flow, api) for api in ('browse', 'booking')}
        hpas = json.loads(base.kube('get', 'hpa', '-n', 'trafficxx', '-o', 'json'))['items']
        require(not any(row['spec']['scaleTargetRef'].get('kind') == 'Deployment'
                        and row['spec']['scaleTargetRef'].get('name') == 'browse' for row in hpas),
                'BROWSE_HPA_PRESENT')
        stage = 'existing_generator_node'
        pod = json.loads(base.kube('get', 'pod', before['browse']['pods'][0]['pod'],
                                  '-n', 'trafficxx', '-o', 'json'))
        generator_node = pod['spec']['nodeName']
        node = json.loads(base.kube('get', 'node', generator_node, '-o', 'json'))
        require(node['metadata']['labels'].get('agentpool') == 'apps'
                and not node['spec'].get('unschedulable')
                and any(row['type'] == 'Ready' and row['status'] == 'True'
                        for row in node['status'].get('conditions', [])), 'EXISTING_APPS_NODE_NOT_READY')
        stage = 'generator_gate_permission'
        require(base.kube('auth', 'can-i', 'create', 'pods/exec', '-n', 'trafficxx') == 'yes',
                'GENERATOR_GATE_EXEC_NOT_ALLOWED')
        app = json.loads(base.kube('get', 'deployment', 'browse', '-n', 'trafficxx', '-o', 'json'))
        container = next(row for row in app['spec']['template']['spec']['containers'] if row['name'] == 'browse')
        limits = container['resources']['limits']
        require(quantity(limits['cpu'], 'cpu') == .5 and quantity(limits['memory'], 'memory') == 256,
                'REVIEW_BROWSE_RESOURCE_LIMITS')
        expected = {row['pod']: {'container': 'browse', 'cpu': .5, 'memory': 256}
                    for row in before['browse']['pods']}
        stage = 'baseline_cpu_memory'
        baseline = sample(metrics(), expected)
        require(set(baseline) == set(expected), 'BROWSE_RESOURCE_METRICS_MISSING')
        require(all(row['cpu_limit_fraction'] < .8 and row['memory_limit_fraction'] < .85
                    for row in baseline.values()), 'BASELINE_RESOURCE_PRESSURE')
        if preflight_only:
            print(json.dumps({'result': 'BROWSE_USERS_PREFLIGHT_OK', 'users': users,
                              'cpu_memory_metrics_available': True, 'inventory_writes': False}))
            return 0
        reference = before['browse']['pods'][0]['image']
        require(re.fullmatch(r'trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}', reference),
                'EXPECTED_IMMUTABLE_IMAGE')
        run_id = uuid4().hex[:12]
        files_name = 'browse-users-' + run_id + '-files'
        stage = 'create_probe_files'
        base.kube('apply', '-f', str(ROOT / 'kubernetes/checks/read-load-serviceaccount.yaml'))
        with tempfile.TemporaryDirectory(prefix='trafficxx-browse-users-') as folder:
            path = Path(folder) / 'targets.json'
            path.write_text(json.dumps({'run_id': run_id, 'users': users, 'targets': {
                api: row['pods'] for api, row in before.items()}}))
            manifest = base.kube('create', 'configmap', files_name, '-n', 'trafficxx',
                                 '--from-file=probe.py=' + str(ROOT / 'scripts/check-browse-users.py'),
                                 '--from-file=read_probe.py=' + str(ROOT / 'scripts/check-read-load.py'),
                                 '--from-file=targets.json=' + str(path), '--dry-run=client', '-o', 'json')
            base.kube('create', '-f', '-', data=manifest)
        stage = 'create_job'
        manifest = base.kube('patch', '--local', '-f', str(ROOT / 'kubernetes/checks/browse-users.yaml'),
                             '--type=json', '--patch', json.dumps([
                                 {'op': 'replace', 'path': '/metadata/name', 'value': job_name},
                                 {'op': 'replace', 'path': '/spec/template/spec/containers/0/image', 'value': reference},
                                 {'op': 'replace', 'path': '/spec/template/spec/nodeName', 'value': generator_node},
                                 {'op': 'replace', 'path': '/spec/template/spec/volumes/0/configMap/name', 'value': files_name}]),
                             '-o', 'json')
        create_requested = True
        job = json.loads(base.kube('create', '-f', '-', '-o', 'json', data=manifest))
        base.kube('patch', 'configmap', files_name, '-n', 'trafficxx', '--type=merge', '--patch', json.dumps({
            'metadata': {'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job', 'name': job_name,
                          'uid': job['metadata']['uid'], 'blockOwnerDeletion': False}]}}))
        print(json.dumps({'result': 'BROWSE_USERS_STARTED', 'job': job_name, 'run_id': run_id,
                          'users': users, 'duration_seconds': 60, 'think_seconds': 2,
                          'max_workload_attempts': users * 30, 'runtime_image': reference,
                          'generator_node': generator_node,
                          'diagnostic_retry': diagnostic_retry,
                          'resource_max_age_seconds': RESOURCE_MAX_AGE,
                          'generator_metrics_warmup_seconds': GENERATOR_METRICS_WARMUP,
                          'inventory_writes': False,
                          'note': 'Only the selected user count; no automatic progression or scaling.'}), flush=True)
        stage = 'observe_job_and_resources'
        deadline, running_since, generator = time.monotonic() + 450, None, None
        while True:
            finished = json.loads(base.kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
            require(finished['metadata']['uid'] == job['metadata']['uid'], 'DIAGNOSTIC_JOB_REPLACED')
            pods = json.loads(base.kube('get', 'pods', '-n', 'trafficxx', '-l', 'job-name=' + job_name, '-o', 'json'))['items']
            require(len(pods) <= 1, 'UNEXPECTED_GENERATOR_REPLICAS')
            require(not finished.get('status', {}).get('failed'), 'DIAGNOSTIC_JOB_FAILED')
            if pods:
                pod = pods[0]
                require(any(row.get('controller') and row['kind'] == 'Job' and row['uid'] == job['metadata']['uid']
                            for row in pod['metadata'].get('ownerReferences', [])), 'GENERATOR_OWNER_CHANGED')
                require(all(row['restartCount'] == 0 for row in pod['status'].get('containerStatuses', [])),
                        'GENERATOR_RESTARTED')
                generator = pod['metadata']['name']
                if pod['status']['phase'] == 'Running' and running_since is None:
                    running_since = time.monotonic()
                if pod['status']['phase'] in ('Running', 'Succeeded', 'Failed'):
                    logs = base.kube('logs', '-n', 'trafficxx', 'job/' + job_name, '-c', 'check')
            if finished.get('status', {}).get('succeeded') == 1:
                require(pods and pods[0]['status']['phase'] == 'Succeeded', 'GENERATOR_COMPLETION_MISSING')
                break
            require(time.monotonic() < deadline, 'DIAGNOSTIC_DEADLINE')
            payload = metrics()
            values = sample(payload, expected)
            require(set(expected) <= set(values), 'BROWSE_RESOURCE_METRICS_MISSING')
            if generator:
                try:
                    values.update(sample(payload, {generator: {'container': 'check', 'cpu': 1, 'memory': 256}}))
                except MetricSampleError as problem:
                    if gate_released or (running_since is not None and time.monotonic() - running_since > GENERATOR_METRICS_WARMUP):
                        raise
                    print(json.dumps({'result': 'BROWSE_USERS_METRICS_WARMUP', **problem.details}), flush=True)
            if running_since is not None and time.monotonic() - running_since > GENERATOR_METRICS_WARMUP:
                require(generator in values, 'GENERATOR_RESOURCE_METRICS_MISSING')
            watch.add(values, time.monotonic())
            if (not gate_released and generator is not None and generator in values
                    and running_since is not None
                    and set(watch.rows) == set(expected) | {generator}
                    and all(len(row['timestamps']) >= 2 for row in watch.rows.values())
                    and all(row['cpu_limit_fraction'] < .8 and row['memory_limit_fraction'] < .85
                            for row in values.values())):
                release_gate(generator, run_id, job_name)
                gate_released = True
                print(json.dumps({'result': 'BROWSE_USERS_RESOURCE_GATE_RELEASED', 'run_id': run_id,
                                  'distinct_fresh_samples_per_pod_minimum': 2}), flush=True)
            time.sleep(5)
        stage = 'validate_measurements'
        require(bool(logs.splitlines()), 'WORKLOAD_LOGS_MISSING')
        outcome = json.loads(logs.splitlines()[-1])
        require(gate_released, 'RESOURCE_GATE_NOT_RELEASED')
        validate(outcome, run_id, users)
        require(generator is not None and set(watch.rows) == set(expected) | {generator}
                and all(len(row['timestamps']) >= 2 for row in watch.rows.values()),
                'INSUFFICIENT_DISTINCT_RESOURCE_SAMPLES')
        stage = 'serving_pod_stability'
        require(before == {api: base.snapshot(flow, api) for api in before}, 'SERVING_PODS_CHANGED')
        for line in logs.splitlines():
            print(line, flush=True)
        print(json.dumps({'result': 'BROWSE_USERS_' + str(users) + '_OK', 'run_id': run_id, 'job': job_name,
                          'resources': watch.summary(), 'serving_pods_unchanged': True,
                          'resource_gate_released': True,
                          'resource_max_age_seconds': RESOURCE_MAX_AGE,
                          'inventory_writes': False, 'latency_slo_tested': False,
                          'note': 'Review results before preparing any higher stage. CPU is window-averaged; peaks are sampled.'}), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        if job is None and create_requested:
            # Resolve a lost create response without stopping a concurrent run.
            try:
                candidate = json.loads(base.kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
                volumes = candidate['spec']['template']['spec']['volumes']
                if any(row.get('configMap', {}).get('name') == files_name for row in volumes):
                    job = candidate
            except Exception:
                pass
        stop_attempted, stop_requested = job is not None, False
        if job is not None:
            try:
                stop_owned_job(job['metadata']['uid'], job_name)
                stop_requested = True
            except Exception:
                pass
            try:
                logs = base.kube('logs', '-n', 'trafficxx', 'job/' + job_name, '-c', 'check')
            except Exception:
                pass
        for line in logs.splitlines():
            print(line, flush=True)
        print(json.dumps({'result': 'BROWSE_USERS_FAILED', 'stage': stage,
                          'error_type': type(error).__name__,
                          'check': str(error) if type(error) is ValueError or isinstance(error, MetricSampleError) else None,
                          'metric_failure': getattr(error, 'details', None),
                          'resource_gate_released': gate_released,
                          'command_exit_code': getattr(error, 'returncode', None),
                          'job': job_name if job else None, 'files': files_name,
                          'generator_stop_attempted': stop_attempted, 'generator_stop_requested': stop_requested,
                          'resources': watch.summary(),
                          'note': 'Inspect this diagnostic Job and logs; do not start overlapping runs. No serving replicas were changed.'}), flush=True)
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--users', type=int, choices=(50, 100), default=50)
    parser.add_argument('--diagnostic-retry', action='store_true',
                        help='Use the separate 100-user diagnostic Job; no automatic retries.')
    args = parser.parse_args()
    if args.diagnostic_retry and args.users != 100:
        parser.error('--diagnostic-retry requires --users 100')
    raise SystemExit(execute(preflight_only=args.preflight_only, users=args.users,
                             diagnostic_retry=args.diagnostic_retry))
