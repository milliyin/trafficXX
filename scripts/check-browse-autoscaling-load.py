"""Bounded 250/500-paced-user Browse reads; service routing permits pod changes."""
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


def run(run_id, *, duration=180, users=250, think=2, ramp_seconds=None, http=read.read_url):
    read.require(re.fullmatch(r'[0-9a-f]{12}', run_id) is not None, 'INVALID_RUN_ID')
    read.require(type(users) is int and users in (250, 500) and duration == 180 and think == 2,
                 'UNSUPPORTED_DRILL')
    if ramp_seconds is None:
        ramp_seconds = 60 if users == 500 else 2
    read.require(type(ramp_seconds) is int and
                 (ramp_seconds in (60, 120) if users == 500 else ramp_seconds == 2), 'UNSUPPORTED_RAMP')
    stop, lock = threading.Event(), threading.Lock()
    rows, failures, participants, first_failure = [], Counter(), set(), []
    start = time.monotonic()
    deadline = start + duration

    def user(index):
        if stop.wait(index * ramp_seconds / users):
            return
        for iteration in range(90):
            if stop.is_set() or time.monotonic() >= deadline:
                return
            route = ('general', 'seated')[(index + iteration) % 2]
            began = time.monotonic()
            status, body = None, None
            try:
                status, body = http(URL + '/events/demo-' + route + '/availability', 'scaling-' + run_id)
                read.validate_response('browse', route, status, body)
                read.require(time.monotonic() - began < 2, 'REQUEST_OVER_TWO_SECONDS')
            except Exception as error:
                with lock:
                    failures[str(error) if isinstance(error, read.CheckFailed) else type(error).__name__] += 1
                    if not first_failure:
                        detail = body.get('detail') if isinstance(body, dict) else None
                        code = detail.get('code') if isinstance(detail, dict) else None
                        first_failure.append({'http_status': status, 'route': route,
                            'service_error_code': code if code in ('DATABASE_BUSY', 'DATABASE_UNAVAILABLE') else None,
                            'phase_elapsed_seconds': round(time.monotonic() - start, 3),
                            'latency_ms': round((time.monotonic() - began) * 1000, 3)})
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
        'scheduled_duration_seconds': duration, 'start_ramp_seconds': ramp_seconds,
        'think_seconds': think, 'max_workload_attempts': users * 90,
        'attempts': len(rows), 'status_counts': dict(Counter(str(r[0]) for r in rows)), 'errors': dict(failures),
        'first_failure': first_failure[0] if first_failure else None,
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
        result = run(sys.argv[1], users=int(sys.argv[2]) if len(sys.argv) >= 3 else 250,
                     ramp_seconds=int(sys.argv[3]) if len(sys.argv) == 4 else None)
        raise SystemExit(0 if result['result'] == 'BROWSE_SCALING_LOAD_OK' else 1)
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_SCALING_LOAD_FAILED', 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1)
