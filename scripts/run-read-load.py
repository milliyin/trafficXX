"""Cloud Shell runner: create one bounded HTTP-only Job, then verify pod stability."""
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def command(args, data=None, timeout=30):
    return subprocess.check_output(args, input=data, text=True, timeout=timeout,
                                   stderr=subprocess.DEVNULL).strip()


def kube(*args, data=None, timeout=30):
    prefix = ['kubectl'] if args[0] == 'wait' else ['kubectl', '--request-timeout=20s']
    return command([*prefix, *args], data=data, timeout=timeout)


def snapshot(flow, api):
    deployment = json.loads(kube('get', 'deployment', api, '-n', 'trafficxx', '-o', 'json'))
    items = json.loads(kube('get', 'pods', '-n', 'trafficxx', '-l',
                            'app.kubernetes.io/name=trafficxx-' + api, '-o', 'json'))['items']
    if len(items) != 2:
        raise ValueError('Expected two stable API pods')
    owner = next(row for row in items[0]['metadata']['ownerReferences'] if row.get('controller'))
    replica_set = json.loads(kube('get', 'replicaset', owner['name'], '-n', 'trafficxx', '-o', 'json'))
    pods = flow.select_pods(deployment, items, replica_set, name=api)
    rows = []
    for pod in pods:
        status = next(row for row in pod['status']['containerStatuses'] if row['name'] == api)
        container = next(row for row in pod['spec']['containers'] if row['name'] == api)
        if not status.get('ready') or type(status.get('restartCount')) is not int:
            raise ValueError('API container not ready')
        rows.append({'api': api, 'pod': pod['metadata']['name'], 'ip': pod['status']['podIP'],
                     'uid': pod['metadata']['uid'], 'restarts': status['restartCount'],
                     'image': container['image']})
    return {'generation': deployment['metadata']['generation'], 'pods': rows}


def validate_measurements(outcome, run_id):
    if (outcome.get('result') != 'READ_LOAD_WORKLOAD_OK' or outcome.get('run_id') != run_id
            or outcome.get('inventory_writes') is not False
            or outcome.get('availability_before_and_after') != 100
            or outcome.get('pool_processes_unchanged') is not True
            or outcome.get('latency_slo_tested') is not False
            or len(outcome.get('phases', [])) != 2):
        raise ValueError('Incomplete workload evidence')
    for phase, workers in zip(outcome['phases'], (1, 4)):
        if (phase.get('workers_per_api') != workers or phase.get('scheduled_duration_seconds') != 30
                or set(phase.get('apis', {})) != {'browse', 'booking'}):
            raise ValueError('Unexpected workload phase')
        for api, values in phase['apis'].items():
            count = values.get('attempts')
            if (type(count) is not int or not 1 <= count <= workers * 60
                    or values.get('valid_responses') != count or values.get('errors')
                    or values.get('status_counts') != {'200' if api == 'browse' else '404': count}):
                raise ValueError('Incomplete response evidence')
            latencies = [values['latency_ms'][key] for key in ('p50', 'p95', 'p99', 'max')]
            if (any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                    for value in latencies) or latencies != sorted(latencies)):
                raise ValueError('Invalid response-time measurements')


def execute():
    stage, job_name, files_name = 'preflight', 'read-load-baseline', None
    try:
        if command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85':
            raise ValueError('Wrong subscription')
        if kube('config', 'current-context') != 'trafficxx-lab-aks':
            raise ValueError('Wrong context')
        if kube('get', 'job', job_name, '-n', 'trafficxx', '--ignore-not-found', '-o', 'name'):
            raise ValueError('Inspect the existing read-load Job before another run')
        spec = importlib.util.spec_from_file_location('pod_checks', ROOT / 'scripts/check-browse-concurrency.py')
        flow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flow)
        before = {api: snapshot(flow, api) for api in ('browse', 'booking')}
        reference = before['booking']['pods'][0]['image']
        if not re.fullmatch(r'trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}', reference):
            raise ValueError('Expected an immutable Booking runtime image')
        run_id = uuid4().hex[:12]
        files_name = 'read-load-' + run_id + '-files'
        config = {'run_id': run_id, 'targets': {api: row['pods'] for api, row in before.items()}}
        stage = 'create_files'
        kube('apply', '-f', str(ROOT / 'kubernetes/checks/read-load-serviceaccount.yaml'))
        with tempfile.TemporaryDirectory(prefix='trafficxx-read-load-') as folder:
            targets = Path(folder) / 'targets.json'
            targets.write_text(json.dumps(config))
            manifest = kube('create', 'configmap', files_name, '-n', 'trafficxx',
                            '--from-file=probe.py=' + str(ROOT / 'scripts/check-read-load.py'),
                            '--from-file=targets.json=' + str(targets), '--dry-run=client', '-o', 'json')
            kube('create', '-f', '-', data=manifest)
        stage = 'create_job'
        patch = [
            {'op': 'replace', 'path': '/metadata/name', 'value': job_name},
            {'op': 'replace', 'path': '/spec/template/spec/containers/0/image', 'value': reference},
            {'op': 'replace', 'path': '/spec/template/spec/volumes/0/configMap/name', 'value': files_name}]
        manifest = kube('patch', '--local', '-f', str(ROOT / 'kubernetes/checks/read-load.yaml'),
                        '--type=json', '--patch', json.dumps(patch), '-o', 'json')
        job = json.loads(kube('create', '-f', '-', '-o', 'json', data=manifest))
        stage = 'attach_file_cleanup'
        owners = {'metadata': {'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'Job',
                  'name': job_name, 'uid': job['metadata']['uid'], 'blockOwnerDeletion': False}]}}
        kube('patch', 'configmap', files_name, '-n', 'trafficxx', '--type=merge', '--patch', json.dumps(owners))
        print(json.dumps({'result': 'READ_LOAD_STARTED', 'job': job_name, 'run_id': run_id,
                          'phases_seconds': [30, 30], 'max_workload_concurrency': 8,
                          'max_workload_attempts': 600, 'inventory_writes': False,
                          'runtime_image': reference,
                          'serving_images': {api: sorted({row['image'] for row in state['pods']})
                                             for api, state in before.items()}}), flush=True)
        stage = 'wait_job'
        try:
            kube('wait', '-n', 'trafficxx', '--for=condition=complete', 'job/' + job_name,
                 '--timeout=240s', timeout=270)
        finally:
            logs = kube('logs', '-n', 'trafficxx', 'job/' + job_name, '-c', 'check')
            for line in logs.splitlines():
                if line.strip():
                    print(line, flush=True)
        stage = 'validate_job'
        outcome = json.loads(logs.splitlines()[-1])
        validate_measurements(outcome, run_id)
        finished = json.loads(kube('get', 'job', job_name, '-n', 'trafficxx', '-o', 'json'))
        pods = json.loads(kube('get', 'pods', '-n', 'trafficxx', '-l', 'job-name=' + job_name, '-o', 'json'))['items']
        if (outcome.get('result') != 'READ_LOAD_WORKLOAD_OK' or outcome.get('run_id') != run_id
                or finished['metadata']['uid'] != job['metadata']['uid']
                or finished['status'].get('succeeded') != 1 or len(pods) != 1
                or pods[0]['status']['phase'] != 'Succeeded'
                or not any(row.get('controller') and row['kind'] == 'Job'
                           and row['uid'] == job['metadata']['uid']
                           for row in pods[0]['metadata'].get('ownerReferences', []))
                or any(row['restartCount'] != 0 for row in pods[0]['status']['containerStatuses'])):
            raise ValueError('Workload completion evidence missing')
        stage = 'postflight'
        if before != {api: snapshot(flow, api) for api in before}:
            raise ValueError('Serving rollout, pod or restart changed during workload')
        print(json.dumps({'result': 'READ_LOAD_BASELINE_OK', 'run_id': run_id, 'job': job_name,
                          'serving_pods_unchanged': True, 'inventory_writes': False,
                          'note': 'Use preceding phase measurements; no latency SLO or write-load claim.'}), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'result': 'READ_LOAD_FAILED', 'stage': stage,
                          'error_type': type(error).__name__, 'job': job_name, 'files': files_name,
                          'note': 'Inspect this Job; do not start overlapping runs. No serving replicas were changed.'}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(execute())
