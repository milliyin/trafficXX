"""In-cluster read-only stage: 50, 100 or 250 paced users for 60 seconds, no retries."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import read_probe as read

USERS = 50
DURATION = 60
THINK = 2


def wait_for_resource_gate(config, path=Path('/state/start'), sleep=time.sleep, clock=time.monotonic):
    read.targets_from(config)  # Validate before waiting; no HTTP is sent here.
    print(json.dumps({'result': 'BROWSE_USERS_WAITING_FOR_RESOURCE_SAMPLES',
                      'run_id': config['run_id'], 'timeout_seconds': 240}), flush=True)
    deadline = clock() + 240
    while True:
        if path.exists():
            read.require(path.read_text().strip() == config['run_id'], 'RESOURCE_GATE_RUN_MISMATCH')
            return
        read.require(clock() < deadline, 'RESOURCE_GATE_TIMEOUT')
        sleep(1)


def execute(config):
    wait_for_resource_gate(config)
    return run(config)


SERVICE_ERROR_CODES = frozenset(('DATABASE_BUSY', 'DATABASE_UNAVAILABLE'))
POOL_KEYS = ('pool_waiting_limit', 'pool_min', 'pool_max', 'pool_size', 'pool_available', 'requests_waiting',
             'requests_num', 'connections_num', *read.ERROR_KEYS)
TIMING_PHASES = ('acquire', 'body', 'return', 'availability_query')


def query_timing_snapshot(stats):
    keys = tuple('timing_' + phase + '_' + suffix for phase in TIMING_PHASES
                 for suffix in ('count', 'total_us')) + ('timing_event_query_count', 'timing_units_query_count')
    read.require(all(type(stats.get(key)) is int and stats[key] >= 0 for key in keys),
                 'DEPLOY_SINGLE_QUERY_TIMERS_FIRST')
    read.require(stats['timing_availability_query_count'] >= 2, 'SINGLE_QUERY_PREFLIGHT_NOT_OBSERVED')


def query_timing_deltas(before, after, *, allow_empty=False):
    query_timing_snapshot(after)
    read.require(all(after['timing_' + phase + '_count'] == before['timing_' + phase + '_count']
                     for phase in ('event_query', 'units_query')), 'LEGACY_QUERY_COUNTER_INCREASED')
    result = {}
    for phase in TIMING_PHASES:
        prefix = 'timing_' + phase + '_'
        count = after[prefix + 'count'] - before[prefix + 'count']
        total = after[prefix + 'total_us'] - before[prefix + 'total_us']
        read.require(count >= 0 and total >= 0 and (count > 0 or (allow_empty and total == 0)),
                     'SINGLE_QUERY_TIMINGS_INCOMPLETE')
        result[phase] = {'observations': count, 'total_us': total,
                         'mean_ms': round(total / count / 1000, 3) if count else None}
    return result


def safe_pool(row):
    return {'instance_id': row['instance_id'],
            'statistics': {key: row['statistics'][key] for key in (*POOL_KEYS, *read.REJECTION_KEYS)
                           if key in row['statistics']}}


def snapshot_pool(target, customer, http, pool_size=4):
    return (read.pool_snapshot(target, customer, http, expected_pool_max=8) if pool_size == 8
            else read.pool_snapshot(target, customer, http))


def failure_pools(targets, initial, customer, http, pool_size=4):
    observations = []
    for target in targets:
        name = target['pod']
        row = {'pod': name, 'before': safe_pool(initial[name])}
        try:
            after = snapshot_pool(target, customer, http, pool_size)
            row['after'] = safe_pool(after)
            row['same_process'] = after['instance_id'] == initial[name]['instance_id']
            if row['same_process']:
                deltas = {key: after['statistics'][key] - initial[name]['statistics'][key]
                          for key in ('requests_num', 'connections_num', *read.ERROR_KEYS, *read.REJECTION_KEYS)
                          if key in after['statistics'] and key in initial[name]['statistics']}
                row['counter_deltas'] = deltas if all(n >= 0 for n in deltas.values()) else None
                if 'timing_availability_query_count' in initial[name]['statistics']:
                    try:
                        row['timings'] = query_timing_deltas(initial[name]['statistics'], after['statistics'],
                                                            allow_empty=True)
                    except Exception as error:
                        row['timing_error_type'] = type(error).__name__
            else:
                row['counter_deltas'] = None
        except Exception as error:
            row['snapshot_error_type'] = type(error).__name__
        observations.append(row)
    return observations


def generator_memory(path=Path('/proc/self/status')):
    # Linux process RSS includes resident thread stacks; this is not cgroup usage.
    try:
        rows = dict(line.split(':', 1) for line in path.read_text().splitlines() if ':' in line)
        parts = rows['VmRSS'].split()
        if len(parts) != 2 or parts[1] != 'kB':
            return None
        rss, threads = int(parts[0]), int(rows['Threads'].strip())
        if rss < 0 or threads < 1:
            return None
        return {'rss_mib': round(rss / 1024, 3), 'threads': threads}
    except (OSError, KeyError, ValueError):
        return None


def run(config, http=read.read_json, service=read.read_url, *, duration=DURATION,
        think=THINK, users=None):
    targets = read.targets_from(config)['browse']
    configured_users = config.get('users', USERS)
    users = configured_users if users is None else users
    pool_size = config.get('pool_size', 4)
    read.require(type(pool_size) is int and pool_size in (4, 8)
                 and (pool_size == 4 or (users == 250 and config.get('single_query_comparison') is True)),
                 'INVALID_POOL_SIZE_COMPARISON')
    read.require(type(users) is int and users in (50, 100, 250) and users == configured_users
                 and duration > 0 and think > 0, 'INVALID_STAGE')
    if users == 250:
        read.require(config.get('require_warm_pool') is True
                     and config.get('require_rejection_counters') is True
                     and config.get('required_queue_limit') == 16
                     and config.get('client_memory_comparison') is True,
                     'LARGER_STAGE_REQUIRES_ALL_GUARDS')
    if config.get('client_memory_comparison'):
        read.require(getattr(read, 'HTTP_CLIENT_POLICY', None) ==
                     'thread-local-http-only-fresh-connections-v1', 'UPDATE_READ_CLIENT_FIRST')
    if config.get('single_query_comparison'):
        read.require(users in (100, 250) and config.get('client_memory_comparison') is True
                     and config.get('require_warm_pool') is True
                     and config.get('require_rejection_counters') is True
                     and config.get('required_queue_limit') == 16, 'INVALID_SINGLE_QUERY_COMPARISON')
    max_attempts = users * 30
    customer = 'lab-browse-users-' + config['run_id']
    for target in targets:
        for route in ('general', 'seated'):
            status, body = http(target, '/events/demo-' + route + '/availability', customer)
            read.validate_response('browse', route, status, body)
    initial = {target['pod']: snapshot_pool(target, customer, http, pool_size) for target in targets}
    if config.get('single_query_comparison'):
        for row in initial.values():
            query_timing_snapshot(row['statistics'])
    if config.get('require_warm_pool'):
        read.require(all(row['statistics']['pool_min'] == pool_size and row['statistics']['pool_size'] == pool_size
                         for row in initial.values()), 'WARM_POOL_NOT_READY')
    error_keys = read.ERROR_KEYS
    if config.get('require_rejection_counters'):
        read.require(all(all(key in row['statistics'] for key in read.REJECTION_KEYS)
                         for row in initial.values()), 'DEPLOY_POOL_REJECTION_COUNTERS_FIRST')
        error_keys += read.REJECTION_KEYS
    required_queue = config.get('required_queue_limit')
    if required_queue is not None:
        read.require(type(required_queue) is int and required_queue in (8, 16), 'INVALID_QUEUE_COMPARISON')
        read.require(all(row['statistics'].get('pool_waiting_limit', 8) == required_queue
                         for row in initial.values()), 'DEPLOY_REQUESTED_QUEUE_LIMIT_FIRST')
    memory_start = generator_memory()
    memory_samples = []
    peaks = {target['pod']: {'size': initial[target['pod']]['statistics']['pool_size'],
                             'waiting': initial[target['pod']]['statistics']['requests_waiting']}
             for target in targets}
    waiting_since = {target['pod']: None for target in targets}
    results, failures, first_failure = [], [], []
    lock, stop = threading.Lock(), threading.Event()
    active, peak_active = 0, 0
    begun = time.monotonic()
    deadline = begun + duration

    def observe():
        try:
            while not stop.is_set():
                memory = generator_memory()
                if memory is not None:
                    memory_samples.append(memory)
                for target in targets:
                    if stop.is_set():
                        return
                    name = target['pod']
                    row = snapshot_pool(target, customer, http, pool_size)
                    before, values = initial[name], row['statistics']
                    read.require(row['instance_id'] == before['instance_id'], 'BROWSE_PROCESS_CHANGED')
                    read.require(all(values[key] == before['statistics'][key] for key in error_keys),
                                 'POOL_ERROR_COUNTER_INCREASED')
                    peaks[name]['size'] = max(peaks[name]['size'], values['pool_size'])
                    peaks[name]['waiting'] = max(peaks[name]['waiting'], values['requests_waiting'])
                    now = time.monotonic()
                    if values['requests_waiting']:
                        if waiting_since[name] is None:
                            waiting_since[name] = now
                        read.require(now - waiting_since[name] < 10, 'SAMPLED_POOL_WAITING_TEN_SECONDS')
                    else:
                        waiting_since[name] = None
                stop.wait(1)
        except Exception as error:
            with lock:
                failures.append(str(error) if isinstance(error, read.CheckFailed) else type(error).__name__)
            stop.set()

    def user(index):
        nonlocal active, peak_active
        # Spread initial requests across one think interval, avoiding a cold burst.
        if stop.wait(think * index / users):
            return
        for iteration in range(min(30, max_attempts // users)):
            if stop.is_set() or time.monotonic() >= deadline:
                return
            route = ('general', 'seated')[(index + iteration) % 2]
            start = time.perf_counter()
            status, error, service_code = None, None, None
            with lock:
                active += 1
                peak_active = max(active, peak_active)
            try:
                status, body = service('http://browse.trafficxx.svc.cluster.local/events/demo-'
                                       + route + '/availability', customer)
                if status != 200:
                    detail = body.get('detail') if isinstance(body, dict) else None
                    code = detail.get('code') if isinstance(detail, dict) else None
                    service_code = code if isinstance(code, str) and code in SERVICE_ERROR_CODES else 'UNRECOGNIZED'
                read.validate_response('browse', route, status, body)
                read.require(time.perf_counter() - start <= 2, 'REQUEST_OVER_TWO_SECONDS')
            except Exception as problem:
                error = str(problem) if isinstance(problem, read.CheckFailed) else type(problem).__name__
                stop.set()
            finally:
                elapsed = time.perf_counter() - start
                with lock:
                    active -= 1
                    results.append((index, elapsed, status, route, error, service_code))
                    if error and not first_failure:
                        first_failure.append({'http_status': status, 'route': route,
                                              'service_error_code': service_code,
                                              'check': error, 'latency_ms': round(elapsed * 1000, 3),
                                              'phase_elapsed_seconds': round(time.monotonic() - begun, 3)})
            if error or stop.wait(min(think, max(0, deadline - time.monotonic()))):
                return

    print(json.dumps({'result': 'BROWSE_USERS_PHASE_STARTED', 'run_id': config['run_id'],
                      'users': users, 'duration_seconds': duration, 'think_seconds': think,
                      'max_workload_attempts': max_attempts}), flush=True)
    with ThreadPoolExecutor(max_workers=users + 1) as executor:
        observer = executor.submit(observe)
        futures = [executor.submit(user, index) for index in range(users)]
        try:
            for future in futures:
                future.result(timeout=duration + 10)
            stop.wait(max(0, deadline - time.monotonic()))
        finally:
            elapsed = time.monotonic() - begun
            stop.set()
            observer.result(timeout=15)
    memory_end = generator_memory()
    memory_rows = [row for row in (memory_start, *memory_samples, memory_end) if row is not None]
    memory_report = {'source': '/proc/self/status', 'scope': 'Process RSS; not cgroup memory',
                     'start': memory_start, 'end': memory_end, 'samples': len(memory_rows),
                     'sampled_max_rss_mib': max((row['rss_mib'] for row in memory_rows), default=None),
                     'sampled_max_threads': max((row['threads'] for row in memory_rows), default=None)}
    errors = Counter(row[4] for row in results if row[4])
    summary = {'users': users, 'users_with_requests': len({row[0] for row in results}),
               'attempts': len(results), 'valid_responses': len(results) - sum(errors.values()),
               'errors': dict(errors), 'status_counts': dict(Counter(str(row[2]) for row in results)),
               'routes': dict(Counter(row[3] for row in results)),
               'latency_ms': read.percentiles([row[1] for row in results]) if results else None,
               'elapsed_seconds': round(elapsed, 3),
               'observed_requests_per_second': round(len(results) / elapsed, 3) if elapsed > 0 else 0,
               'sampled_peak_workload_http_in_flight': peak_active, 'observer_checks': failures, 'generator_memory': memory_report}
    if errors or failures or summary['users_with_requests'] != users:
        diagnostics = {'first_failure': first_failure[0] if first_failure else None,
                       'service_error_codes': dict(Counter(row[5] for row in results if row[5])),
                       'pool_snapshots_after_workload_stopped': failure_pools(targets, initial, customer, http, pool_size)}
        print(json.dumps({'result': 'BROWSE_USERS_PHASE_FAILED', 'run_id': config['run_id'],
                          **summary, **diagnostics}), flush=True)
        raise read.CheckFailed('BROWSE_USERS_WORKLOAD_OR_POOL_STOP')
    pools = []
    for target in targets:
        name = target['pod']
        after = snapshot_pool(target, customer, http, pool_size)
        before = initial[name]
        read.require(after['instance_id'] == before['instance_id'], 'BROWSE_PROCESS_CHANGED')
        read.require(all(after['statistics'][key] == before['statistics'][key] for key in error_keys),
                     'POOL_ERROR_COUNTER_INCREASED')
        read.require(after['statistics']['requests_num'] >= before['statistics']['requests_num']
                     and after['statistics']['connections_num'] >= before['statistics']['connections_num'],
                     'POOL_COUNTER_RESET')
        pools.append({'pod': name, 'pool_max': pool_size,
                      'pool_waiting_limit': before['statistics'].get('pool_waiting_limit', 8),
                      'pool_error_counters_unchanged': True,
                      'sampled_peak_size': max(peaks[name]['size'], after['statistics']['pool_size']),
                      'sampled_peak_waiting': max(peaks[name]['waiting'], after['statistics']['requests_waiting']),
                      'borrows_delta': after['statistics']['requests_num'] - before['statistics']['requests_num'],
                      'connection_attempts_delta': after['statistics']['connections_num'] - before['statistics']['connections_num']})
        if config.get('single_query_comparison'):
            pools[-1]['timings'] = query_timing_deltas(before['statistics'], after['statistics'])
        for route in ('general', 'seated'):
            status, body = http(target, '/events/demo-' + route + '/availability', customer)
            read.validate_response('browse', route, status, body)
    return {'result': 'BROWSE_USERS_WORKLOAD_OK', 'run_id': config['run_id'], **summary,
            'pools': pools, 'inventory_writes': False, 'availability_before_and_after': 100,
            'pool_processes_unchanged': True, 'latency_slo_tested': False,
            'warm_pool_verified': bool(config.get('require_warm_pool')),
            'rejection_counters_verified': bool(config.get('require_rejection_counters')),
            'verified_queue_limit': required_queue,
            'initial_pools': {name: safe_pool(row) for name, row in initial.items()},
            'http_client_policy': getattr(read, 'HTTP_CLIENT_POLICY', None),
            'single_query_verified': bool(config.get('single_query_comparison')),
            'pool_size_verified': pool_size,
            'scope': 'In-cluster Browse Service reads; paced users are not requests/s.'}


if __name__ == '__main__':
    try:
        print(json.dumps(execute(json.loads(Path('/checks/targets.json').read_text()))), flush=True)
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_USERS_WORKLOAD_FAILED', 'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, read.CheckFailed) else None}), flush=True)
        raise SystemExit(1) from None
