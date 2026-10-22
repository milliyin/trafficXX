"""User-run idle hold test with read-only SQL observation in short-lived Jobs."""
import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import time
from uuid import UUID, uuid4

spec = importlib.util.spec_from_file_location(
    'expiry_worker_transport', Path(__file__).with_name('check-booking-concurrency.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
require = probe.require


def worker_snapshot():
    deployment = json.loads(probe.kubectl('get', 'deployment', 'expiry-worker', '-n', 'trafficxx', '-o', 'json'))
    require(deployment['spec']['replicas'] == 1
            and deployment['status'].get('readyReplicas') == 1
            and deployment['status'].get('updatedReplicas') == 1
            and deployment['status'].get('observedGeneration', 0) >= deployment['metadata']['generation'],
            'Expiry worker rollout must be stable and ready')
    pod_spec = deployment['spec']['template']['spec']
    container = pod_spec['containers'][0]
    require(pod_spec['serviceAccountName'] == 'expiry'
            and container['command'] == ['python', '-u', '-m', 'trafficxx.expiry'],
            'Unexpected worker identity or command')
    require(container['image'] == 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:'
            '22a28c8c6cc45354a327b8b7d5423cb4b169daa49a3efe320c392173dc923dbc',
            'Unexpected worker image; review this experiment before reuse')
    return deployment


def observer_job(worker, reservation_id, phase, name):
    pod_spec = copy.deepcopy(worker['spec']['template']['spec'])
    pod_spec.update(serviceAccountName='migrator', restartPolicy='Never')
    container = pod_spec['containers'][0]
    container['name'] = 'observe'
    container['command'] = ['python', '-u', '-c',
                            Path(__file__).with_name('observe-expiry-state.py').read_text(encoding='utf-8')]
    for key in ('readinessProbe', 'livenessProbe', 'startupProbe', 'ports'):
        container.pop(key, None)
    for env in container['env']:
        if env['name'] == 'PGUSER':
            env['value'] = 'trafficxx-lab-migrator'
    container['env'].extend([{'name': 'CHECK_RESERVATION_ID', 'value': str(UUID(reservation_id))},
                             {'name': 'CHECK_PHASE', 'value': phase}])
    return {'apiVersion': 'batch/v1', 'kind': 'Job',
            'metadata': {'name': name, 'namespace': 'trafficxx'},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 180, 'ttlSecondsAfterFinished': 600,
                     'template': {'metadata': {'labels': {'azure.workload.identity/use': 'true'}},
                                  'spec': pod_spec}}}


def inspect(worker, reservation_id, phase):
    name = 'expiry-state-' + uuid4().hex[:16]
    manifest = observer_job(worker, reservation_id, phase, name)
    try:
        subprocess.run(['kubectl', '--request-timeout=20s', 'create', '-f', '-'],
                       input=json.dumps(manifest), text=True, check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        subprocess.run(['kubectl', 'wait', '-n', 'trafficxx', '--for=condition=complete',
                        'job/' + name, '--timeout=180s'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=190)
        logs = probe.kubectl('logs', '-n', 'trafficxx', 'job/' + name, '-c', 'observe')
        result = json.loads(logs)
        require(result.get('result') == 'EXPIRY_STATE_OK' and result.get('phase') == phase
                and result.get('reservation_id') == reservation_id, 'Observer did not verify state')
        return result
    finally:
        # Delete only this run's random observer Job, never the worker or data.
        try:
            probe.kubectl('delete', 'job', name, '-n', 'trafficxx', '--ignore-not-found', '--wait=false')
        except Exception:
            pass  # Job deadline/TTL still bounds and cleans it up.


def experiment(call, customer, key, snapshot=worker_snapshot, inspect_state=inspect):
    worker = snapshot()
    body = {'event_id': 'demo-seated', 'seats': ['A2']}
    reservation_id = None
    started = False
    finished = False
    try:
        started = True
        status, held = call(0, 'POST', '/reservations', customer, key, body)
        if status in (200, 201) and 'id' in held:
            reservation_id = str(UUID(held['id']))
        require(status == 201 and held['status'] == 'HELD' and held['seats'] == ['A2'],
                'Expected a fresh A2 hold')
        duration = (datetime.fromisoformat(held['expires_at'].replace('Z', '+00:00'))
                    - datetime.fromisoformat(held['created_at'].replace('Z', '+00:00'))).total_seconds()
        require(290 <= duration <= 310, 'Expected five-minute hold duration')
        baseline = inspect_state(worker, reservation_id, 'held')
        print(json.dumps({'result': 'WAITING_FOR_WORKER_EXPIRY', 'reservation_id': reservation_id,
                          'seat': 'A2', 'wait_seconds': 310, 'baseline': baseline}), flush=True)
        deadline = time.monotonic() + 310
        while time.monotonic() < deadline:
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        # No Booking/Browse calls after creation: observer executes SELECT only.
        evidence = inspect_state(worker, reservation_id, 'expired')
        final_worker = snapshot()
        require(final_worker['metadata']['uid'] == worker['metadata']['uid']
                and final_worker['metadata']['generation'] == worker['metadata']['generation'],
                'Worker Deployment changed during experiment')
        finished = True
        return {'result': 'EXPIRY_WORKER_OK', 'reservation_id': reservation_id,
                'seat': 'A2', 'final_status': evidence['status'],
                'remaining_assignments': evidence['assignments'],
                'held_outbox_rows': evidence['held_messages'],
                'expired_outbox_rows': evidence['expired_messages'],
                'observation': 'read_only_sql', 'expiry_triggering_api_calls_after_create': 0}
    finally:
        if started and not finished:
            # Only on failure, resolve an unknown create with its original key.
            # This cleanup may invoke API expiry; never report it as worker proof.
            if reservation_id is None:
                status, row = call(0, 'POST', '/reservations', customer, key, body)
                if status in (200, 201):
                    reservation_id = str(UUID(row['id']))
                else:
                    require(status == 409 and row == {'detail': {'code': 'SEAT_UNAVAILABLE'}},
                            'Unknown hold outcome; inspect original key')
            if reservation_id:
                status, row = call(0, 'POST', '/reservations/' + reservation_id + '/cancel', customer)
                require((status == 200 and row.get('status') == 'CANCELLED')
                        or (status == 409 and row == {'detail': {'code': 'RESERVATION_EXPIRED'}}),
                        'Hold cleanup not verified; inspect original key')


if __name__ == '__main__':
    raise SystemExit(probe.main(experiment_fn=experiment, failure_result='EXPIRY_WORKER_FAILED'))
