"""User-run Cloud Shell experiment; two pod-specific forwards, one shared key."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import threading
import tempfile
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4


def require(condition, message):
    if not condition:
        raise ValueError(message)


def kubectl(*args):
    return subprocess.check_output(['kubectl', '--request-timeout=20s', *args],
                                   text=True, timeout=30)


def select_pods(deployment, pods, replica_set):
    require(deployment['spec']['replicas'] == 2, 'Expected two Booking replicas')
    require(deployment['status'].get('observedGeneration', 0)
            >= deployment['metadata']['generation'], 'Rollout not observed')
    require(deployment['status'].get('updatedReplicas') == 2
            and deployment['status'].get('readyReplicas') == 2,
            'Booking rollout must be ready')
    require(any(o.get('controller') and o['uid'] == deployment['metadata']['uid']
                and o['kind'] == 'Deployment'
                for o in replica_set['metadata']['ownerReferences']),
            'ReplicaSet does not belong to Booking')
    require(len(pods) == 2, 'Expected exactly two stable Booking pods')
    for pod in pods:
        require(not pod['metadata'].get('deletionTimestamp')
                and pod['status']['phase'] == 'Running'
                and any(c['type'] == 'Ready' and c['status'] == 'True'
                        for c in pod['status'].get('conditions', [])), 'Pod not ready')
        require(any(o.get('controller') and o['kind'] == 'ReplicaSet'
                    and o['uid'] == replica_set['metadata']['uid']
                    for o in pod['metadata']['ownerReferences']), 'Pod owner mismatch')
    require(len({p['spec']['nodeName'] for p in pods}) == 2,
            'Expected Booking pods on separate nodes')
    return sorted(p['metadata']['name'] for p in pods)


def experiment(call, customer, key):
    body = {'event_id': 'demo-general', 'quantity': 2}
    ids = set()
    started = False
    gate = threading.Barrier(2)

    def create(index):
        gate.wait(timeout=10)
        return call(index, 'POST', '/reservations', customer, key, body)

    try:
        started = True
        # Await both outcomes even if one request fails; cleanup then resolves
        # ambiguous creates using the same customer/key, never a new key.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, i) for i in range(2)]
            outcomes = []
            failures = []
            for future in futures:
                try:
                    status, row = future.result(timeout=35)
                    outcomes.append((status, row))
                    if 'id' in row:
                        ids.add(str(UUID(row['id'])))
                except Exception as error:
                    failures.append(error)
        if failures:
            raise failures[0]
        require(sorted(s for s, _ in outcomes) == [200, 201],
                'Expected one create and one duplicate')
        require(len(ids) == 1 and outcomes[0][1] == outcomes[1][1],
                'Replicas returned different reservations or expiry')
        held = outcomes[0][1]
        require(held['status'] == 'HELD' and held['quantity'] == 2
                and held['event_id'] == 'demo-general', 'Unexpected hold')
        reservation_id = next(iter(ids))
        for index in range(2):
            status, row = call(index, 'GET', '/reservations/' + reservation_id, customer)
            require(status == 200 and row == held, 'Replica read disagrees')
        return {'result': 'BOOKING_CONCURRENCY_OK', 'reservation_id': reservation_id,
                'http_statuses': sorted(s for s, _ in outcomes),
                'same_reservation_and_expiry': True, 'both_replica_reads': True,
                'final_status': 'CANCELLED'}
    finally:
        if started:
            # Recover the original ID if both POST responses were lost. If a
            # create never committed, this may create one diagnostic hold and
            # immediately cancel it. Preserve all returned IDs if a bug made two.
            if not ids:
                for index in range(2):
                    try:
                        status, row = call(index, 'POST', '/reservations', customer, key, body)
                        if status in (200, 201):
                            ids.add(str(UUID(row['id'])))
                            break
                    except Exception:
                        pass
            require(bool(ids), 'Unable to resolve hold outcome; inspect using the same key')
            failed_cleanup = []
            for reservation_id in ids:
                try:
                    status, row = call(0, 'POST', '/reservations/' + reservation_id + '/cancel', customer)
                    require(status == 200 and row.get('status') == 'CANCELLED', 'Cancellation failed')
                    for index in range(2):
                        status, row = call(index, 'GET', '/reservations/' + reservation_id, customer)
                        require(status == 200 and row.get('status') == 'CANCELLED',
                                'Cancellation readback failed')
                except Exception:
                    failed_cleanup.append(reservation_id)
            require(not failed_cleanup, 'Cancellation not verified: ' + ','.join(failed_cleanup))


def main(experiment_fn=experiment, failure_result='BOOKING_CONCURRENCY_FAILED', recovery_note=None):
    processes = []
    logs = []
    customer, key = 'lab-concurrent-' + uuid4().hex, uuid4().hex
    pods = []
    try:
        require(kubectl('config', 'current-context').strip() == 'trafficxx-lab-aks',
                'Select the trafficxx-lab-aks context')
        deployment = json.loads(kubectl('get', 'deployment', 'booking', '-n', 'trafficxx', '-o', 'json'))
        items = json.loads(kubectl('get', 'pods', '-n', 'trafficxx', '-l',
                                  'app.kubernetes.io/name=trafficxx-booking', '-o', 'json'))['items']
        require(len(items) == 2, 'Expected two stable Booking pods')
        owner = next(o for o in items[0]['metadata']['ownerReferences'] if o.get('controller'))
        rs = json.loads(kubectl('get', 'replicaset', owner['name'], '-n', 'trafficxx', '-o', 'json'))
        pods = select_pods(deployment, items, rs)
        for index, pod in enumerate(pods):
            log = tempfile.TemporaryFile()
            logs.append(log)
            processes.append(subprocess.Popen(
                ['kubectl', 'port-forward', '-n', 'trafficxx', '--address=127.0.0.1',
                 'pod/' + pod, str(18012 + index) + ':8000'],
                stdout=log, stderr=log))
        for index, process in enumerate(processes):
            for attempt in range(80):
                require(process.poll() is None, 'Pod port forward exited before binding')
                logs[index].seek(0)
                if f'Forwarding from 127.0.0.1:{18012 + index}'.encode() in logs[index].read():
                    break
                require(attempt < 79, 'Pod port forward did not bind')
                time.sleep(0.25)

        def call(index, method, path, identity, request_key=None, body=None):
            require(processes[index].poll() is None, 'Port forward exited')
            headers = {'X-Customer-Id': identity}
            if request_key:
                headers['Idempotency-Key'] = request_key
            headers['Content-Type'] = 'application/json'
            request = urllib.request.Request(
                f'http://127.0.0.1:{18012 + index}' + path, headers=headers, method=method,
                data=json.dumps(body).encode() if body is not None else None)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                response = opener.open(request, timeout=20)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.code, json.load(response)

        for index in range(2):
            for attempt in range(60):
                try:
                    status, row = call(index, 'GET', '/readyz', customer)
                    require(status == 200 and row == {'status': 'ready'}, 'Replica not ready')
                    break
                except (OSError, ValueError):
                    if attempt == 59:
                        raise
                    time.sleep(0.25)
        result = experiment_fn(call, customer, key)
        result['pods'] = pods
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'result': failure_result,
                          'error_type': type(error).__name__, 'customer': customer,
                          'idempotency_key': key, 'pods': pods,
                          'note': recovery_note or
                                  'Inspect with the same key. An uncancelled hold expires '
                                  'logically after five minutes; do not reset inventory.'}))
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == '__main__':
    raise SystemExit(main())
