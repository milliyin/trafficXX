"""In-cluster 250-paced-user Browse drill; service routing permits pod changes."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import threading
import time

import read_probe as read

URL = 'http://browse.trafficxx.svc.cluster.local'


def run(run_id, *, duration=180, users=250, think=2, http=read.read_url):
    read.require(re.fullmatch(r'[0-9a-f]{12}', run_id) is not None, 'INVALID_RUN_ID')
    read.require(users == 250 and duration == 180 and think == 2, 'UNSUPPORTED_DRILL')
    stop, lock = threading.Event(), threading.Lock()
    rows, failures, participants = [], Counter(), set()
    start = time.monotonic()
    deadline = start + duration

    def user(index):
        if stop.wait(index * think / users):
            return
        for iteration in range(90):
            if stop.is_set() or time.monotonic() >= deadline:
                return
            route = ('general', 'seated')[(index + iteration) % 2]
            began = time.monotonic()
            status = None
            try:
                status, body = http(URL + '/events/demo-' + route + '/availability', 'scaling-' + run_id)
                read.validate_response('browse', route, status, body)
                read.require(time.monotonic() - began < 2, 'REQUEST_OVER_TWO_SECONDS')
            except Exception as error:
                with lock:
                    failures[str(error) if isinstance(error, read.CheckFailed) else type(error).__name__] += 1
                stop.set()
            finally:
                with lock:
                    participants.add(index)
                    rows.append((status, time.monotonic() - began))
            if stop.wait(think):
                return

    with ThreadPoolExecutor(max_workers=users) as executor:
        list(executor.map(user, range(users)))
    result = {'result': 'BROWSE_SCALING_LOAD_OK' if not failures and len(participants) == users else 'BROWSE_SCALING_LOAD_FAILED',
        'run_id': run_id, 'users': users, 'users_with_requests': len(participants),
        'attempts': len(rows), 'status_counts': dict(Counter(str(r[0]) for r in rows)), 'errors': dict(failures),
        'latency_ms': read.percentiles([r[1] for r in rows]) if rows else {},
        'elapsed_seconds': round(time.monotonic() - start, 3), 'inventory_writes': False,
        'latency_slo_tested': False}
    print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    try:
        deadline = time.monotonic() + 240
        print(json.dumps({'result': 'BROWSE_SCALING_WAITING_FOR_RESOURCES'}), flush=True)
        while not Path('/state/start').exists():
            read.require(time.monotonic() < deadline, 'RESOURCE_GATE_TIMEOUT')
            time.sleep(1)
        result = run(sys.argv[1])
        raise SystemExit(0 if result['result'] == 'BROWSE_SCALING_LOAD_OK' else 1)
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_SCALING_LOAD_FAILED', 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1)
