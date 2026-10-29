"""Bounded 250/500/1000-paced-user Browse reads; service routing permits pod changes."""
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


def user_window(index, users=1000):
    """Absolute arrival/departure offsets: 120s ramp, 180s hold, 120s decline."""
    read.require(type(index) is int and type(users) is int and users == 1000 and 0 <= index < users,
                 'INVALID_PROFILE_USER')
    return index * 120 / users, 300 + (index + 1) * 120 / users


def profile_phase(elapsed):
    return 'ramp_up' if elapsed < 120 else 'hold' if elapsed < 300 else 'ramp_down'


def run(run_id, *, duration=180, users=250, think=2, ramp_seconds=None, http=read.read_url, ramp_down=False):
    read.require(re.fullmatch(r'[0-9a-f]{12}', run_id) is not None, 'INVALID_RUN_ID')
    read.require(type(users) is int and users in (250, 500, 1000) and think == 2 and
                 (duration == 180 or (users == 1000 and duration == 300) or
                  (users == 1000 and duration == 420 and ramp_down)),
                 'UNSUPPORTED_DRILL')
    read.require(not ramp_down or (users == 1000 and duration == 420), 'UNSUPPORTED_PROFILE')
    if ramp_seconds is None:
        ramp_seconds = 120 if users == 1000 else 60 if users == 500 else 2
    read.require(type(ramp_seconds) is int and
                 (ramp_seconds == 120 if users == 1000 else
                  ramp_seconds in (60, 120) if users == 500 else ramp_seconds == 2), 'UNSUPPORTED_RAMP')
    stop, lock = threading.Event(), threading.Lock()
    rows, failures, participants, first_failure = [], Counter(), set(), []
    active, peak_active, progress = 0, 0, []
    start = time.monotonic()
    deadline = start + duration

    def user(index):
        nonlocal active, peak_active
        arrival, departure = user_window(index, users) if ramp_down else (index * ramp_seconds / users, duration)
        delay = max(0, start + arrival - time.monotonic()) if ramp_down else arrival
        if stop.wait(delay) or (ramp_down and time.monotonic() >= start + departure):
            return
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            work(index, start + departure)
        finally:
            with lock:
                active -= 1

    def work(index, user_deadline):
        for iteration in range(duration // think):
            if stop.is_set() or time.monotonic() >= min(deadline, user_deadline):
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
                    elapsed = time.monotonic() - start
                    rows.append((status, time.monotonic() - began, profile_phase(began - start)))
                    if ramp_down and (not progress or elapsed - progress[-1]['elapsed_seconds'] >= 5):
                        sample = {'elapsed_seconds': round(elapsed, 3), 'phase': profile_phase(elapsed),
                                  'active_users': active, 'attempts': len(rows)}
                        progress.append(sample)
                        print(json.dumps({'result': 'BROWSE_USER_PROFILE_PROGRESS', 'run_id': run_id, **sample}), flush=True)
            pause = min(think, max(0, user_deadline - time.monotonic())) if ramp_down else think
            if stop.wait(pause):
                return

    with ThreadPoolExecutor(max_workers=users) as executor:
        list(executor.map(user, range(users)))
    profile_ok = (not ramp_down or (peak_active == users and active == 0 and time.monotonic() - start >= duration))
    result = {'result': 'BROWSE_SCALING_LOAD_OK' if not failures and len(participants) == users and profile_ok else 'BROWSE_SCALING_LOAD_FAILED',
        'run_id': run_id, 'users': users, 'users_with_requests': len(participants),
        'scheduled_duration_seconds': duration, 'start_ramp_seconds': ramp_seconds,
        'think_seconds': think, 'max_workload_attempts': users * (duration // think),
        'attempts': len(rows), 'status_counts': dict(Counter(str(r[0]) for r in rows)), 'errors': dict(failures),
        'first_failure': first_failure[0] if first_failure else None,
        'latency_ms': read.percentiles([r[1] for r in rows]) if rows else {},
        'elapsed_seconds': round(time.monotonic() - start, 3), 'inventory_writes': False,
        'latency_slo_tested': False}
    if ramp_down:
        progress.append({'elapsed_seconds': round(time.monotonic() - start, 3), 'phase': 'ramp_down',
                         'active_users': active, 'attempts': len(rows)})
        result.update(ramp_down_seconds=120, hold_seconds=180, peak_active_users=peak_active,
                      active_users_after_workload=active, progress_samples=progress,
                      phases={phase: {'attempts': sum(r[2] == phase for r in rows),
                                     'status_counts': dict(Counter(str(r[0]) for r in rows if r[2] == phase)),
                                     'latency_ms': read.percentiles([r[1] for r in rows if r[2] == phase])
                                         if any(r[2] == phase for r in rows) else {}}
                              for phase in ('ramp_up', 'hold', 'ramp_down')})
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
                     ramp_seconds=int(sys.argv[3]) if len(sys.argv) >= 4 else None,
                     duration=int(sys.argv[4]) if len(sys.argv) >= 5 else 180,
                     ramp_down=len(sys.argv) == 6 and sys.argv[5] == 'ramp-down')
        raise SystemExit(0 if result['result'] == 'BROWSE_SCALING_LOAD_OK' else 1)
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_SCALING_LOAD_FAILED', 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1)
