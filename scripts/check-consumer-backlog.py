"""User-run Cloud Shell lab: pause, accumulate two events, restore and verify."""
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

NAMESPACE = 'trafficxx'
SUBSCRIPTION = '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'
SELECTOR = 'app.kubernetes.io/name=trafficxx-consumer'
CURRENT_CHECK = 'preflight'


def command(args, payload=None):
    result = subprocess.run(args, input=payload, text=True, capture_output=True,
                            timeout=210, check=False)
    if result.returncode:
        # CLI errors may contain request detail; output only a safe failure class.
        raise RuntimeError('Cloud Shell command failed')
    return result.stdout.strip()


def deployment(call):
    return json.loads(call(['kubectl', 'get', 'deployment/consumer', '-n', NAMESPACE, '-o', 'json']))


def queue(call):
    state = json.loads(call([
        'az', 'servicebus', 'queue', 'show', '--resource-group', 'trafficxx-lab-rg',
        '--namespace-name', 'trafficxx-sb-e8708a85', '--name', 'reservation-events',
        '--query', '{active:countDetails.activeMessageCount,dead_letters:countDetails.deadLetterMessageCount,total:messageCount}',
        '--output', 'json']))
    if set(state) != {'active', 'dead_letters', 'total'} or any(
            type(v) is not int or v < 0 for v in state.values()):
        raise ValueError('Unexpected queue count response')
    return state


def wait_queue(call, sleep, expected):
    for _ in range(24):
        state = queue(call)
        if state['dead_letters']:
            raise ValueError('Unexpected dead letters')
        if state == {'active': expected, 'dead_letters': 0, 'total': expected}:
            return state
        sleep(5)
    raise TimeoutError('Expected native queue counts were not observed')


def restore(call, uid):
    current = deployment(call)
    if current['metadata']['uid'] != uid or current['spec']['replicas'] not in (0, 1):
        raise ValueError('Deployment changed; inspect before restoring replicas')
    if current['spec']['replicas'] == 0:
        call(['kubectl', 'scale', 'deployment/consumer', '-n', NAMESPACE,
              '--replicas=1', '--current-replicas=0'])
    call(['kubectl', 'rollout', 'status', 'deployment/consumer', '-n', NAMESPACE, '--timeout=180s'])


def run(call=command, sleep=time.sleep):
    global CURRENT_CHECK
    CURRENT_CHECK = 'preflight'
    if call(['az', 'account', 'show', '--query', 'id', '-o', 'tsv']) != SUBSCRIPTION:
        raise ValueError('Select the sponsorship subscription')
    if call(['kubectl', 'config', 'current-context']) != 'trafficxx-lab-aks':
        raise ValueError('Select the lab cluster context')
    original = deployment(call)
    if (original['spec']['replicas'] != 1 or original.get('status', {}).get('readyReplicas') != 1
            or original['spec']['selector']['matchLabels'] != {'app.kubernetes.io/name': 'trafficxx-consumer'}):
        raise ValueError('Expected one Ready consumer with the reviewed selector')
    uid = original['metadata']['uid']
    before = queue(call)
    if before != {'active': 0, 'dead_letters': 0, 'total': 0}:
        raise ValueError('Start from an empty queue with no dead letters')
    for name in ('publisher', 'booking'):
        call(['kubectl', 'rollout', 'status', 'deployment/' + name, '-n', NAMESPACE, '--timeout=60s'])
    call(['kubectl', 'get', 'configmap/consumer-drain-check-files', '-n', NAMESPACE, '-o', 'name'])
    paused = False
    try:
        CURRENT_CHECK = 'pause_consumer'
        # Set before command: even an ambiguous scale failure runs restoration.
        paused = True
        call(['kubectl', 'scale', 'deployment/consumer', '-n', NAMESPACE,
              '--replicas=0', '--current-replicas=1'])
        for _ in range(60):
            pods = json.loads(call(['kubectl', 'get', 'pods', '-n', NAMESPACE, '-l', SELECTOR, '-o', 'json']))
            if not pods['items']:
                break
            sleep(2)
        else:
            raise TimeoutError('Consumer pods did not terminate')
        CURRENT_CHECK = 'create_and_cancel_hold'
        flow = json.loads(call(['bash', 'scripts/check-booking-flow.sh']))
        if flow.get('result') != 'BOOKING_FLOW_OK' or flow.get('final_status') != 'CANCELLED':
            raise ValueError('Booking flow did not finish with a cancelled reservation')
        CURRENT_CHECK = 'observe_backlog_while_paused'
        backlog = wait_queue(call, sleep, 2)
        current = deployment(call)
        pods = json.loads(call(['kubectl', 'get', 'pods', '-n', NAMESPACE, '-l', SELECTOR, '-o', 'json']))
        if current['metadata']['uid'] != uid or current['spec']['replicas'] != 0 or pods['items']:
            raise ValueError('Consumer was not paused for the backlog observation')
        print(json.dumps({'result': 'CONSUMER_BACKLOG_OBSERVED', 'queue': backlog,
                          'consumer_replicas': 0, 'consumer_pods': 0,
                          'reservation_id': flow['reservation_id'], 'final_status': 'CANCELLED'}), flush=True)
    finally:
        if paused:
            # Includes KeyboardInterrupt and failed booking/queue checks.
            stage = CURRENT_CHECK
            CURRENT_CHECK = 'restore_consumer'
            restore(call, uid)
            CURRENT_CHECK = stage
    CURRENT_CHECK = 'observe_native_drain'
    after = wait_queue(call, sleep, 0)
    CURRENT_CHECK = 'read_only_database_observer'
    # Reuse the installed observer ConfigMap and create a distinct short-lived Job.
    job = json.loads(call(['kubectl', 'create', '-f', 'kubernetes/checks/consumer-drain.yaml',
                           '--dry-run=client', '-o', 'json']))
    name = 'backlog-drain-' + uuid4().hex[:12]
    job['metadata']['name'] = name
    call(['kubectl', 'apply', '-f', '-'], json.dumps(job))
    try:
        call(['kubectl', 'wait', '-n', NAMESPACE, '--for=condition=complete', 'job/' + name, '--timeout=180s'])
        stored = json.loads(call(['kubectl', 'logs', '-n', NAMESPACE, 'job/' + name, '-c', 'check']))
        if stored.get('result') != 'CONSUMER_DRAIN_OK' or stored.get('read_only') is not True:
            raise ValueError('Stored inbox/projection drain was not verified')
    finally:
        call(['kubectl', 'delete', 'job/' + name, '-n', NAMESPACE, '--ignore-not-found=true', '--wait=false'])
    return {'result': 'CONSUMER_BACKLOG_RECOVERY_OK', 'reservation_id': flow['reservation_id'],
            'queue_before': before, 'queue_while_paused': backlog, 'queue_after': after,
            'restored_replicas': 1, 'stored_state': stored}


def main():
    try:
        # Cloud Shell invokes this script from any working directory.
        import os
        os.chdir(Path(__file__).resolve().parents[1])
        print(json.dumps(run()))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'result': 'CONSUMER_BACKLOG_RECOVERY_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__,
                          'recovery': 'Inspect consumer replicas/readiness. To restore the lab: kubectl scale deployment/consumer -n trafficxx --replicas=1; kubectl rollout status deployment/consumer -n trafficxx --timeout=180s'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
