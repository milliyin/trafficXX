"""Cloud Shell only: owned temporary queue-32 comparison, then restore queue 16."""
import argparse
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('queue_scaling', ROOT / 'scripts/browse-autoscaling.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
MARKER = 'trafficxx.io/queue-comparison'


def submit_patch(operations):
    result = subprocess.run(['kubectl', '--request-timeout=20s', 'patch', 'deployment', 'browse',
        '-n', 'trafficxx', '--type=json', '-p', json.dumps(operations)],
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(s.prewarm_patch_failure(result.stderr).replace('PREWARM_', 'QUEUE_')) from None


def marker_for(app, expected_run=None):
    raw = app['metadata'].get('annotations', {}).get(MARKER, '')
    s.require(isinstance(raw, str) and len(raw) <= 256, 'QUEUE_MARKER_INVALID')
    try:
        row = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('QUEUE_MARKER_INVALID') from None
    s.require(isinstance(row, dict) and set(row) == {'run_id', 'template_sha', 'deployment_uid'}
              and isinstance(row.get('run_id'), str) and re.fullmatch(r'[0-9a-f]{12}', row['run_id']) is not None
              and row['template_sha'] == s.trial.fingerprint(app['spec']['template'])
              and row['deployment_uid'] == app['metadata']['uid']
              and (expected_run is None or row['run_id'] == expected_run), 'QUEUE_OWNERSHIP_CHANGED')
    return row


def patch_queue(desired, *, expected_run):
    s.require(desired in (16, 32) and re.fullmatch(r'[0-9a-f]{12}', expected_run), 'UNREVIEWED_QUEUE_CHANGE')
    s.trial.no_active_load()
    s.owned_hpa(pressure_target='3', scale_up_seconds=30)
    raw = s.trial.deployment()
    env = raw['spec']['template']['spec']['containers'][0]['env']
    indexes = [i for i, row in enumerate(env) if row['name'] == 'BROWSE_DB_POOL_MAX_WAITING']
    s.require(len(indexes) == 1 and env[indexes[0]].get('value') in ('16', '32'), 'QUEUE_POLICY_CHANGED')
    old = int(env[indexes[0]]['value'])
    with s.queue_expectation(old):
        app = s.deployment(raw['metadata']['uid'])
    # Recompute against the exact fresh template protected by the CAS tests.
    env = app['spec']['template']['spec']['containers'][0]['env']
    indexes = [i for i, row in enumerate(env) if row['name'] == 'BROWSE_DB_POOL_MAX_WAITING']
    s.require(len(indexes) == 1 and env[indexes[0]].get('value') == str(old), 'QUEUE_POLICY_CHANGED')
    annotations = app['metadata'].get('annotations', {})
    if desired == 16 and old == 16 and MARKER not in annotations:
        return
    if desired == 32:
        s.require(old == 16 and MARKER not in annotations and app['spec']['replicas'] == 2,
                  'QUEUE_COMPARISON_ALREADY_CONFIGURED')
    else:
        s.require(old == 32, 'QUEUE_POLICY_CHANGED')
        marker_for(app, expected_run)
    target = deepcopy(app['spec']['template'])
    target['spec']['containers'][0]['env'][indexes[0]]['value'] = str(desired)
    operations = [
        {'op': 'test', 'path': '/metadata/uid', 'value': app['metadata']['uid']},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']},
        {'op': 'test', 'path': '/spec/template', 'value': app['spec']['template']},
        {'op': 'test', 'path': '/metadata/annotations', 'value': annotations}
        if 'annotations' in app['metadata'] else {'op': 'add', 'path': '/metadata/annotations', 'value': {}},
        {'op': 'replace', 'path': '/spec/template/spec/containers/0/env/' + str(indexes[0]) + '/value',
         'value': str(desired)},
        {'op': 'add' if desired == 32 else 'remove', 'path': '/metadata/annotations/trafficxx.io~1queue-comparison',
         **({'value': json.dumps({'run_id': expected_run, 'template_sha': s.trial.fingerprint(target),
                                  'deployment_uid': app['metadata']['uid']})}
            if desired == 32 else {})}]
    submit_patch(operations)
    print(s.base.kube('rollout', 'status', 'deployment/browse', '-n', 'trafficxx', '--timeout=600s', timeout=620), flush=True)
    with s.queue_expectation(desired), s.prometheus() as query:
        s.wait_baseline(query)
        current = s.deployment(app['metadata']['uid'])
        s.require(current['spec']['template'] == target, 'QUEUE_TEMPLATE_CHANGED')
        if desired == 32:
            marker_for(current, expected_run)
        else:
            s.require(MARKER not in current['metadata'].get('annotations', {}), 'QUEUE_MARKER_NOT_REMOVED')
    s.emit('BROWSE_QUEUE_POLICY_CHANGED', queue_per_pod=desired, connections_per_pod=8, inventory_writes=False)


def restore(*, expected_run=None):
    s.trial.context()
    s.trial.no_active_load()
    app = s.trial.deployment()
    if MARKER not in app['metadata'].get('annotations', {}):
        with s.queue_expectation(16):
            current = s.deployment(app['metadata']['uid'])
        s.owned_hpa(pressure_target='3', scale_up_seconds=30)
        s.require(current['spec']['replicas'] == current.get('status', {}).get('readyReplicas') == 2,
                  'QUEUE_BASELINE_NOT_RESTORED')
        s.emit('BROWSE_QUEUE_COMPARISON_RESTORED', queue_per_pod=16, ready_replicas=2, connections_per_pod=8)
        return
    marker = marker_for(app, expected_run)
    with s.queue_expectation(32):
        s.restore_prewarm()
    patch_queue(16, expected_run=marker['run_id'])
    s.trial.health()
    s.emit('BROWSE_QUEUE_COMPARISON_RESTORED', queue_per_pod=16, ready_replicas=2, connections_per_pod=8)


def run(*, reuse_image=False):
    s.trial.context()
    s.trial.no_active_load()
    s.require(not s.base.kube('get', 'job', s.QUEUE32_PROFILE_JOB, '-n', 'trafficxx',
                              '--ignore-not-found', '-o', 'name'), 'INSPECT_EXISTING_DRILL_JOB')
    s.require(MARKER not in s.trial.deployment()['metadata'].get('annotations', {}), 'INSPECT_EXISTING_QUEUE_MARKER')
    s.trial.health()
    s.emit('BROWSE_QUEUE32_WORKFLOW_STARTED', job=s.QUEUE32_PROFILE_JOB,
           queue_per_pod=[16, 32, 16], connections_per_pod=8,
           note=('Using the deployed instrumented image; ' if reuse_image else 'Building and deploying recent-check image; ')
                + 'running the 120/180/120 profile, reporting and restoring. Keep Cloud Shell open.')
    # New app code admits queue 32 only for Browse. Build the recent-check
    # variant, but initially retain queue 16 and the live KEDA policy.
    if not reuse_image:
        s.base.command(['bash', str(ROOT / 'scripts/build-browse-image.sh'), '--recent-checks'], timeout=900)
        s.deploy_image()
    run_id = uuid4().hex[:12]
    primary = None
    changed = False
    try:
        changed = True  # Covers a lost patch response that actually applied.
        patch_queue(32, expected_run=run_id)
        with s.queue_expectation(32):
            baseline = json.loads(s.base.command(['python3', str(ROOT / 'scripts/check-browse-timing.py'),
                '--pool-size', '8', '--queue-limit', '32', '--require-health-check', '--check-interval', '1'],
                timeout=180).splitlines()[-1])
            s.require(baseline.get('result') == 'BROWSE_TIMING_BASELINE_OK', 'QUEUE_BASELINE_NOT_VERIFIED')
            s.emit('BROWSE_QUEUE32_BASELINE_OK', timing=baseline)
            s.drill_prewarmed(profile=True, measured='queue32')
    except (Exception, KeyboardInterrupt) as error:
        primary = error
    finally:
        # Preserve the queue-32 historical window before its rollout to queue 16.
        try:
            if s.base.kube('get', 'job', s.QUEUE32_PROFILE_JOB, '-n', 'trafficxx', '--ignore-not-found', '-o', 'name'):
                s.diagnose_500(users=1000, distribution=True, duration=420, prewarmed=True,
                               measure_all=True, measured='queue32')
        except Exception as error:
            s.emit('BROWSE_QUEUE32_REPORT_UNAVAILABLE', **s.failure_details(error))
            if primary is None:
                primary = error
        if changed:
            try:
                restore(expected_run=run_id)
            except (Exception, KeyboardInterrupt) as error:
                s.emit('BROWSE_QUEUE32_RESTORE_REQUIRED', **s.failure_details(error),
                       note='After the Job is terminal, run browse-queue-comparison.py restore. Queue 32 may remain.')
                if primary is None:
                    primary = error
    if primary is not None:
        raise primary
    s.emit('BROWSE_QUEUE32_COMPARISON_OK', queue_restored=16, ready_replicas=2, inventory_writes=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'restore', 'diagnose', 'run-retained', 'diagnose-retained',
                                         'run-wait-measured', 'diagnose-wait-measured',
                                         'run-network-measured', 'diagnose-network-measured'))
    args = parser.parse_args()
    if args.action.endswith('-retained'):
        s.DURABLE_EVIDENCE = True
        s.QUEUE32_PROFILE_JOB = 'browse-users-1000-queue32-retained'
    if args.action.endswith('-wait-measured'):
        s.DURABLE_EVIDENCE = True
        s.WAIT_TIME_MEASUREMENT = True
        s.QUEUE32_PROFILE_JOB = 'browse-users-1000-queue32-waittime'
    if args.action.endswith('-network-measured'):
        s.DURABLE_EVIDENCE = True
        s.WAIT_TIME_MEASUREMENT = True
        s.QUEUE32_PROFILE_JOB = 'browse-users-1000-queue32-network'
    try:
        if args.action == 'run-network-measured':
            run(reuse_image=True)
        elif args.action in ('run', 'run-retained', 'run-wait-measured'):
            run()
        elif args.action == 'restore':
            restore()
        else:
            s.diagnose_500(users=1000, distribution=True, duration=420, prewarmed=True,
                           measure_all=True, measured='queue32')
        return 0
    except (Exception, KeyboardInterrupt) as error:
        s.emit('BROWSE_QUEUE_COMPARISON_FAILED', action=args.action, **s.failure_details(error),
               observer=getattr(error, 'observer_context', None))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
