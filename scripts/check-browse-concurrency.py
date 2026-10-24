"""Bounded, read-only Cloud Shell check of the two serving Browse processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import tempfile
import threading
import time
import urllib.request


class CheckFailed(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def kubectl(*args):
    return subprocess.check_output(['kubectl', '--request-timeout=20s', *args],
                                   text=True, timeout=30, stderr=subprocess.DEVNULL)


def select_pods(deployment, items, replica_set, *, name='browse'):
    require(name in ('browse', 'booking'), 'Unexpected API name')
    require(deployment['metadata']['name'] == name
            and deployment['metadata']['namespace'] == 'trafficxx', 'Unexpected deployment')
    require(deployment['spec']['replicas'] == 2
            and deployment['status'].get('observedGeneration', 0) >= deployment['metadata']['generation']
            and deployment['status'].get('updatedReplicas') == 2
            and deployment['status'].get('readyReplicas') == 2, 'API must have two ready updated replicas')
    require(any(o.get('controller') and o['kind'] == 'Deployment'
                and o['uid'] == deployment['metadata']['uid']
                for o in replica_set['metadata']['ownerReferences']), 'ReplicaSet owner mismatch')
    require(len(items) == 2, 'Expected exactly two stable API pods')
    for pod in items:
        require(not pod['metadata'].get('deletionTimestamp')
                and pod['status']['phase'] == 'Running'
                and any(c['type'] == 'Ready' and c['status'] == 'True'
                        for c in pod['status'].get('conditions', [])), 'API pod not ready')
        require(any(o.get('controller') and o['kind'] == 'ReplicaSet'
                    and o['uid'] == replica_set['metadata']['uid']
                    for o in pod['metadata']['ownerReferences']), 'Pod owner mismatch')
    # Both may be on one node; this experiment does not test node resilience.
    return sorted(items, key=lambda pod: pod['metadata']['name'])


def snapshot(call, index):
    row = call(index, '/internal/database-pool')
    require(isinstance(row.get('instance_id'), str) and len(row['instance_id']) == 32,
            'Missing process instance identifier')
    stats = row['statistics']
    keys = ('pool_min', 'pool_max', 'pool_size', 'pool_available', 'requests_waiting',
            'requests_num', 'requests_errors', 'connections_num', 'connections_errors',
            'connections_lost', 'returns_bad')
    require(all(type(stats.get(key)) is int and stats[key] >= 0 for key in keys),
            'Invalid numeric pool statistics')
    require(stats['pool_min'] in (1, 4) and stats['pool_max'] == 4
            and 1 <= stats['pool_size'] <= 4
            and stats['pool_available'] <= stats['pool_size']
            and stats['requests_waiting'] <= 8, 'Unexpected pool bounds')
    return row


def experiment(call):
    paths = ('/events', '/events/demo-general/availability', '/events/demo-seated/availability')
    baseline = [[call(index, path) for path in paths] for index in range(2)]
    require(baseline[0] == baseline[1], 'Replica demo data disagrees')
    require(isinstance(baseline[0][0], list)
            and {'demo-general', 'demo-seated'} <= {row['id'] for row in baseline[0][0]},
            'Demo events missing')
    general, seated = baseline[0][1:]
    require(general['id'] == 'demo-general' and general['inventory_type'] == 'GENERAL'
            and type(general['available']) is int and general['available'] == 100
            and general['seats'] is None, 'Expected fully available general demo inventory')
    require(seated['id'] == 'demo-seated' and seated['inventory_type'] == 'SEATED'
            and type(seated['available']) is int and seated['available'] == 100
            and isinstance(seated['seats'], list) and len(seated['seats']) == 100
            and set(seated['seats']) == {f'A{n}' for n in range(1, 101)},
            'Expected fully available seated demo inventory')
    before = [snapshot(call, index) for index in range(2)]
    samples = [[], []]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            for index in range(2):
                row = snapshot(call, index)
                require(row['instance_id'] == before[index]['instance_id'], 'Browse process restarted')
                samples[index].append(row['statistics']['pool_size'])
            stop.wait(0.2)

    def read(index, number):
        require(call(index, paths[number % 3]) == baseline[index][number % 3],
                'Incorrect or changed demo response')

    gate = threading.Barrier(8)

    def burst_read(index, number):
        if number < 4:
            gate.wait(timeout=15)
        read(index, number)

    with ThreadPoolExecutor(max_workers=1) as observer:
        future = observer.submit(sample)
        try:
            # Fixed workload:48 reads, at most8 workload clients in flight.
            # The separate statistics observer does not borrow SQL connections.
            # No retries, write endpoints, sleeps inside SQL or autoscaling.
            with ThreadPoolExecutor(max_workers=8) as clients:
                futures = [clients.submit(burst_read, index, number)
                           for number in range(24) for index in range(2)]
                for task in futures:
                    task.result(timeout=90)
            # Six additional sequential requests per pod show reuse after burst.
            for index in range(2):
                for number in range(6):
                    read(index, number)
                require(call(index, '/readyz') == {'status': 'ready'}, 'Browse not ready after burst')
                require(call(index, '/healthz') == {'status': 'alive'}, 'Browse not alive after burst')
        finally:
            stop.set()
            future.result(timeout=25)  # Observer failures must prevent a pass.
    after = [snapshot(call, index) for index in range(2)]
    results = []
    for index in range(2):
        require(after[index]['instance_id'] == before[index]['instance_id'], 'Browse process changed')
        initial, final = before[index]['statistics'], after[index]['statistics']
        loans = final['requests_num'] - initial['requests_num']
        attempts = final['connections_num'] - initial['connections_num']
        require(loans >= 31 and 0 <= attempts < 30, 'Connection reuse not supported by counters')
        for key in ('requests_errors', 'connections_errors', 'connections_lost', 'returns_bad'):
            require(final[key] == initial[key], 'Pool error counter increased')
        require(final['requests_waiting'] == 0, 'Pool borrowers still waiting')
        results.append({'successful_test_reads': 30, 'pool_borrows_delta': loans,
                        'connection_attempts_delta': attempts,
                        'sampled_peak_pool_size': max(samples[index] + [initial['pool_size'], final['pool_size']]),
                        'pool_max': final['pool_max'], 'reuse_supported': True})
    return {'result': 'BROWSE_CONCURRENCY_OK', 'burst_requests': 48,
            'max_workload_http_concurrency': 8, 'sequential_reads': 12,
            'per_pod': results, 'inventory_writes': False,
            'note': 'Bounded port-forward experiment; sampled sizes are not a throughput or latency SLO.'}


def main():
    processes, logs = [], []
    stage = 'preflight'
    try:
        require(subprocess.check_output(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'],
                                       text=True, timeout=30, stderr=subprocess.DEVNULL).strip()
                == '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85', 'Wrong subscription')
        require(kubectl('config', 'current-context').strip() == 'trafficxx-lab-aks', 'Wrong context')
        deployment = json.loads(kubectl('get', 'deployment', 'browse', '-n', 'trafficxx', '-o', 'json'))
        items = json.loads(kubectl('get', 'pods', '-n', 'trafficxx', '-l',
                                  'app.kubernetes.io/name=trafficxx-browse', '-o', 'json'))['items']
        require(len(items) == 2, 'Expected exactly two Browse pods')
        owner = next(o for o in items[0]['metadata']['ownerReferences'] if o.get('controller'))
        rs = json.loads(kubectl('get', 'replicaset', owner['name'], '-n', 'trafficxx', '-o', 'json'))
        pods = select_pods(deployment, items, rs)
        stage = 'port_forward'
        for index, pod in enumerate(pods):
            log = tempfile.TemporaryFile()
            logs.append(log)
            processes.append(subprocess.Popen(
                ['kubectl', 'port-forward', '-n', 'trafficxx', '--address=127.0.0.1',
                 'pod/' + pod['metadata']['name'], f'{18041 + index}:8000'], stdout=log, stderr=log))
        for index, process in enumerate(processes):
            for attempt in range(80):
                require(process.poll() is None, 'Forward exited')
                logs[index].seek(0)
                if f'Forwarding from 127.0.0.1:{18041 + index}'.encode() in logs[index].read():
                    break
                require(attempt < 79, 'Forward did not bind')
                time.sleep(0.25)

        def call(index, path):
            require(processes[index].poll() is None, 'Forward exited')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{18041 + index}' + path, timeout=10) as response:
                require(response.status == 200, 'Unexpected HTTP status')
                data = response.read(65537)
                require(len(data) <= 65536, 'Unexpected response size')
                return json.loads(data)

        stage = 'read_only_experiment'
        result = experiment(call)
        stage = 'pod_stability'
        for original in pods:
            current = json.loads(kubectl('get', 'pod', original['metadata']['name'], '-n', 'trafficxx', '-o', 'json'))
            require(current['metadata']['uid'] == original['metadata']['uid']
                    and not current['metadata'].get('deletionTimestamp')
                    and any(c['type'] == 'Ready' and c['status'] == 'True'
                            for c in current['status'].get('conditions', []))
                    and current['status']['containerStatuses'] == original['status']['containerStatuses'],
                    'Pod identity, image, readiness or restart state changed')
        result['pods'] = [pod['metadata']['name'] for pod in pods]
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_CONCURRENCY_FAILED', 'stage': stage,
                          'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, CheckFailed) else None,
                          'note': 'Read-only test; no inventory cleanup required. Do not reset inventory.'}))
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == '__main__':
    raise SystemExit(main())
