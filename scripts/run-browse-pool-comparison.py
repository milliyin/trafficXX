"""Cloud Shell: temporary eight-slot pool trial, journaled restoration to four."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = Path.home() / 'trafficxx-browse-pool8-journal.json'
JOB = 'browse-users-250-pool8'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


users = load('run-browse-users')
base = users.base


def require(condition, code):
    if not condition:
        raise ValueError(code)


def job_for(duration):
    require(type(duration) is int and duration in (60, 180), 'INVALID_TRIAL_DURATION')
    return JOB if duration == 60 else JOB + '-180s'


def validate_state(state, duration=None):
    saved_duration = state.get('duration_seconds', 60)
    require(state.get('version') == 1 and state.get('job') == job_for(saved_duration)
            and (duration is None or duration == saved_duration), 'UNEXPECTED_JOURNAL')


def deployment():
    return json.loads(base.kube('get', 'deployment', 'browse', '-n', 'trafficxx', '-o', 'json'))


def fingerprint(template):
    return hashlib.sha256(json.dumps(template, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def policy(app, size):
    require(app['metadata']['name'] == 'browse' and app['metadata']['namespace'] == 'trafficxx'
            and app['spec']['replicas'] == 2, 'UNEXPECTED_BROWSE_DEPLOYMENT')
    containers = app['spec']['template']['spec']['containers']
    require(len(containers) == 1 and containers[0]['name'] == 'browse', 'UNEXPECTED_BROWSE_CONTAINER')
    container = containers[0]
    limits = container.get('resources', {}).get('limits', {})
    require(users.quantity(limits.get('cpu', ''), 'cpu') == .5
            and users.quantity(limits.get('memory', ''), 'memory') == 256, 'BROWSE_RESOURCE_LIMITS_CHANGED')
    require(re.fullmatch(r'trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}', container['image']),
            'EXPECTED_IMMUTABLE_IMAGE')
    result = {}
    for name, value in (('BROWSE_DB_POOL_MIN', str(size)), ('BROWSE_DB_POOL_MAX', str(size)),
                        ('BROWSE_DB_POOL_MAX_WAITING', '16')):
        matches = [(index, row) for index, row in enumerate(container.get('env', [])) if row['name'] == name]
        require(len(matches) == 1 and matches[0][1].get('value') == value, 'UNEXPECTED_POOL_POLICY')
        result[name] = matches[0][0]
    return result


def state_for(app, duration_seconds=60):
    indices = policy(app, 4)
    trial = deepcopy(app['spec']['template'])
    for name in ('BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX'):
        trial['spec']['containers'][0]['env'][indices[name]]['value'] = '8'
    return {'version': 1, 'deployment_uid': app['metadata']['uid'],
            'image': app['spec']['template']['spec']['containers'][0]['image'],
            'baseline_hash': fingerprint(app['spec']['template']), 'trial_hash': fingerprint(trial),
            'job': job_for(duration_seconds), 'duration_seconds': duration_seconds,
            'state': 'prepared', 'load_passed': False}


def save(path, state, *, create=False):
    if create:
        with path.open('x', encoding='utf-8') as file:
            os.chmod(path, 0o600)
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
        return
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.',
                                     encoding='utf-8', delete=False) as file:
        temporary = Path(file.name)
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def context():
    require(base.command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'])
            == '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85', 'WRONG_SUBSCRIPTION')
    require(base.kube('config', 'current-context') == 'trafficxx-lab-aks', 'WRONG_CONTEXT')


def no_active_load():
    rows = json.loads(base.kube('get', 'jobs', '-n', 'trafficxx', '-o', 'json'))['items']
    require(not any((row['metadata']['name'].startswith('browse-users-')
                     or row['metadata']['name'] == 'read-load-baseline')
                    and not any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                                for c in row.get('status', {}).get('conditions', [])) for row in rows),
            'OTHER_READ_LOAD_ACTIVE')


def health():
    for script, expected in (('check-browse-http.sh', 'BROWSE_DEMO_HTTP_OK'),
                              ('check-operations-http.sh', 'OPERATIONS_HTTP_OK')):
        args = ['bash', str(ROOT / 'scripts' / script)] + (['--demo'] if 'browse' in script else [])
        output = base.command(args, timeout=90)
        row = json.loads(output.splitlines()[-1])
        require(row.get('result') == expected, 'HEALTH_CHECK_FAILED')
        if 'browse' in script:
            require(row.get('demo_availability') == {'demo-general': 100, 'demo-seated': 100}, 'INVENTORY_NOT_BASELINE')
        else:
            require(row.get('alerts') == [] and row['metrics']['outbox_pending'] == 0
                    and row['metrics']['processing_outstanding'] == 0, 'OPERATIONS_NOT_BASELINE')
        print(output, flush=True)


def verify_pool(size):
    flow, read = users.load('check-browse-concurrency'), users.load('check-read-load')
    def experiment(call):
        for index in range(2):
            row = read.pool_snapshot({'api': 'browse'}, 'pool-comparison',
                lambda target, path, customer: (200, call(index, path)), expected_pool_max=size)
            stats = row['statistics']
            require(stats['pool_min'] == size and stats['pool_size'] == size
                    and stats.get('pool_waiting_limit') == 16, 'SERVING_POOL_POLICY_MISMATCH')
            require(type(stats.get('timing_availability_query_count')) is int, 'SINGLE_QUERY_TIMERS_MISSING')
        return {'result': 'BROWSE_POOL_POLICY_OK', 'pool_min': size, 'pool_max': size,
                'queue_limit': 16, 'inventory_writes': False}
    require(flow.main(experiment_function=experiment, failure_result='BROWSE_POOL_POLICY_FAILED') == 0,
            'SERVING_POOL_CHECK_FAILED')


def patch_pool(app, state, expected, desired):
    require((expected, desired) in ((4, 8), (8, 4)), 'INVALID_POOL_POLICY_TRANSITION')
    validate_state(state)
    require(app['metadata']['uid'] == state['deployment_uid'], 'DEPLOYMENT_REPLACED')
    expected_hash = state['baseline_hash' if expected == 4 else 'trial_hash']
    require(fingerprint(app['spec']['template']) == expected_hash, 'DEPLOYMENT_TEMPLATE_CHANGED')
    require(app['spec']['template']['spec']['containers'][0]['image'] == state['image'], 'IMAGE_CHANGED')
    indices = policy(app, expected)
    changes = [{'op': 'test', 'path': '/metadata/uid', 'value': state['deployment_uid']},
               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']}]
    for name in ('BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX'):
        path = f'/spec/template/spec/containers/0/env/{indices[name]}/value'
        changes.extend(({'op': 'test', 'path': path, 'value': str(expected)},
                        {'op': 'replace', 'path': path, 'value': str(desired)}))
    base.kube('patch', 'deployment', 'browse', '-n', 'trafficxx', '--type=json', '--patch', json.dumps(changes))


def rollout():
    print(base.kube('rollout', 'status', 'deployment/browse', '-n', 'trafficxx', '--timeout=240s', timeout=260), flush=True)
    deadline = time.monotonic() + 60
    flow = users.load('check-browse-concurrency')
    while True:
        try:
            base.snapshot(flow, 'browse')
            return
        except ValueError:
            require(time.monotonic() < deadline, 'BROWSE_PODS_NOT_STABLE_AFTER_ROLLOUT')
            time.sleep(3)


def restore(path, state):
    context()
    # Wait for the owned runner's stop request to take effect; never stop others.
    deadline = time.monotonic() + 45
    while True:
        try:
            no_active_load()
            break
        except ValueError:
            require(time.monotonic() < deadline, 'ACTIVE_LOAD_BLOCKS_RESTORE')
            time.sleep(3)
    app = deployment()
    require(app['metadata']['uid'] == state['deployment_uid'], 'DEPLOYMENT_REPLACED')
    if fingerprint(app['spec']['template']) != state['baseline_hash']:
        patch_pool(app, state, 8, 4)
    else:
        policy(app, 4)
    rollout()
    verify_pool(4)
    health()
    state['state'] = 'restored'
    save(path, state)
    print(json.dumps({'result': 'BROWSE_POOL4_RESTORED', 'pool_min': 4, 'pool_max': 4,
                      'queue_limit': 16, 'journal': str(path), 'health_verified': True}), flush=True)


def wait_metrics():
    flow = users.load('check-browse-concurrency')
    pods = base.snapshot(flow, 'browse')['pods']
    expected = {row['pod']: {'container': 'browse', 'cpu': .5, 'memory': 256} for row in pods}
    deadline = time.monotonic() + 150
    while True:
        try:
            values = users.sample(users.metrics(), expected)
            if set(values) == set(expected):
                require(all(row['cpu_limit_fraction'] < .8 and row['memory_limit_fraction'] < .85
                            for row in values.values()), 'BASELINE_RESOURCE_PRESSURE')
                return
        except users.MetricSampleError:
            pass
        require(time.monotonic() < deadline, 'NEW_POD_RESOURCE_METRICS_NOT_READY')
        time.sleep(5)


def database_metrics(start):
    end = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    capacity = load('inspect-browse-capacity')
    try:
        payload = json.loads(base.command(['az', 'monitor', 'metrics', 'list', '--resource', capacity.RESOURCE,
            '--metric', 'cpu_percent', 'memory_percent', 'active_connections', '--aggregation', 'Average', 'Maximum',
            '--interval', 'PT1M', '--start-time', start, '--end-time', end, '-o', 'json']))
        summary = capacity.summarize_metrics(payload)
        available = all(row['observations'] > 0 for row in summary.values())
        print(json.dumps({'result': 'BROWSE_POOL8_DATABASE_METRICS', 'start_utc': start, 'end_utc': end,
                          'metrics': summary, 'all_metrics_have_samples': available,
                          'note': 'Includes generator warmup; minute aggregation and publication lag may miss brief pressure. '
                                  'No database saturation or capacity verdict.'}), flush=True)
        return available
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_POOL8_DATABASE_METRICS_UNAVAILABLE',
                          'error_type': type(error).__name__, 'start_utc': start, 'end_utc': end}), flush=True)
        return False


def execute(*, journal=None, restore_only=False, preflight_only=False, duration_seconds=60):
    job_name = job_for(duration_seconds)
    if journal is None:
        journal = JOURNAL if duration_seconds == 60 else JOURNAL.with_name('trafficxx-browse-pool8-180s-journal.json')
    state, changed, restored, code, stage = None, False, False, 1, 'preflight'
    try:
        context()
        if restore_only:
            state = json.loads(journal.read_text())
            validate_state(state, duration_seconds)
            restore(journal, state)
            return 0
        require(not journal.exists(), 'INSPECT_EXISTING_POOL_COMPARISON_JOURNAL')
        no_active_load()
        require(not base.kube('get', 'job', job_name, '-n', 'trafficxx', '--ignore-not-found', '-o', 'name'),
                'INSPECT_EXISTING_POOL_COMPARISON_JOB')
        hpas = json.loads(base.kube('get', 'hpa', '-n', 'trafficxx', '-o', 'json'))['items']
        require(not any(row['spec']['scaleTargetRef'].get('name') == 'browse' for row in hpas), 'BROWSE_HPA_PRESENT')
        flow = users.load('check-browse-concurrency')
        before = {api: base.snapshot(flow, api) for api in ('browse', 'booking')}
        app = deployment()
        state = state_for(app, duration_seconds)
        health()
        verify_pool(4)
        expected = {row['pod']: {'container': 'browse', 'cpu': .5, 'memory': 256} for row in before['browse']['pods']}
        values = users.sample(users.metrics(), expected)
        require(set(values) == set(expected) and all(row['cpu_limit_fraction'] < .8
                and row['memory_limit_fraction'] < .85 for row in values.values()), 'BASELINE_RESOURCE_PRESSURE')
        if preflight_only:
            print(json.dumps({'result': 'BROWSE_POOL8_PREFLIGHT_OK', 'inventory_writes': False,
                              'duration_seconds': duration_seconds, 'job': job_name,
                              'journal_created': False, 'pool_policy_changed': False}))
            return 0
        require({api: base.snapshot(flow, api) for api in before} == before, 'SERVING_PODS_CHANGED')
        stage = 'save_rollback_journal'
        save(journal, state, create=True)
        changed = True  # A lost patch response still requires restoration inspection.
        stage = 'apply_pool8'
        patch_pool(deployment(), state, 4, 8)
        rollout()
        verify_pool(8)
        health()
        state['state'] = 'pool8_ready'
        save(journal, state)
        stage = 'new_pod_metrics'
        wait_metrics()
        stage = 'pool8_workload'
        state['load_runner_start_utc'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        save(journal, state)
        code = users.execute(users=250, single_query_comparison=True, pool_size=8,
                             duration_seconds=duration_seconds)
        state['load_passed'] = code == 0
        state['database_metric_samples_available'] = database_metrics(state['load_runner_start_utc'])
        save(journal, state)
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'result': 'BROWSE_POOL8_COMPARISON_FAILED', 'stage': stage,
                          'error_type': type(error).__name__, 'check': str(error) if type(error) is ValueError else None,
                          'journal': str(journal), 'note': 'Keep the journal. No inventory cleanup is needed.'}), flush=True)
        code = 1
    finally:
        if changed:
            try:
                restore(journal, state)
                restored = True
            except (Exception, KeyboardInterrupt) as error:
                print(json.dumps({'result': 'BROWSE_POOL4_RESTORE_FAILED', 'error_type': type(error).__name__,
                                  'check': str(error) if type(error) is ValueError else None,
                                  'journal': str(journal),
                                  'recovery': 'Keep journal; after diagnostic loads stop, rerun with --restore --duration '
                                              + str(duration_seconds) + '. '
                                              'Template/identity conflicts require inspection; they are not overwritten.'}), flush=True)
                code = 1
    if changed:
        print(json.dumps({'result': 'BROWSE_POOL8_COMPARISON_OK' if code == 0 and restored else 'BROWSE_POOL8_COMPARISON_NOT_PASSED',
                          'load_passed': state['load_passed'], 'baseline_pool_restored': restored,
                          'duration_seconds': duration_seconds, 'job': job_name,
                          'database_metric_samples_available': state.get('database_metric_samples_available', False),
                          'journal': str(journal), 'inventory_writes': False}), flush=True)
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--preflight-only', action='store_true')
    modes.add_argument('--restore', action='store_true')
    parser.add_argument('--duration', type=int, choices=(60, 180), default=60)
    args = parser.parse_args()
    raise SystemExit(execute(restore_only=args.restore, preflight_only=args.preflight_only,
                             duration_seconds=args.duration))
