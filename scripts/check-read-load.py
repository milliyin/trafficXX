"""Bounded in-cluster GET workload; no credentials, reservation writes or retries."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HTTP_CLIENT_POLICY = 'thread-local-http-only-fresh-connections-v1'
PERIOD = 0.5
DURATION = 30
REJECTION_KEYS = ('rejections_queue_full', 'rejections_timeout', 'rejections_closed')
ERROR_KEYS = ('requests_errors', 'connections_errors', 'connections_lost', 'returns_bad')


class CheckFailed(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_json(target, path, customer):
    return read_url('http://' + target['ip'] + ':8000' + path, customer)


_client_state = threading.local()


def build_http_opener():
    # Do not use build_opener: Python 3.12 constructs its default HTTPS handler
    # and TLS context even for HTTP-only calls. This probe has only HTTP targets.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    NoRedirect(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def http_opener():
    # Handler construction is reused only within one thread. urllib still opens
    # and closes a fresh HTTP connection for every request. No cookie/auth handler.
    if not hasattr(_client_state, 'opener'):
        _client_state.opener = build_http_opener()
    return _client_state.opener


def read_url(url, customer):
    # Fresh Request and HTTP connection per call; no application-response cache.
    require(urllib.parse.urlsplit(url).scheme == 'http', 'HTTP_ONLY_READ_CLIENT')
    opener = http_opener()
    request = urllib.request.Request(url,
                                     headers={'X-Customer-Id': customer}, method='GET')
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(65537)
        require(len(body) <= 65536, 'RESPONSE_TOO_LARGE')
        return response.code, json.loads(body)


def targets_from(config):
    require(re.fullmatch(r'[0-9a-f]{12}', config['run_id']) is not None, 'INVALID_RUN_ID')
    require(set(config['targets']) == {'browse', 'booking'}, 'EXPECTED_BOTH_APIS')
    addresses = set()
    for api, targets in config['targets'].items():
        require(len(targets) == 2, 'EXPECTED_TWO_PODS_PER_API')
        for target in targets:
            require(target['api'] == api and target['pod'].startswith(api + '-'), 'WRONG_API_TARGET')
            require(ipaddress.ip_address(target['ip']) in ipaddress.ip_network('10.244.0.0/16'),
                    'TARGET_OUTSIDE_POD_NETWORK')
            require(target['ip'] not in addresses, 'DUPLICATE_TARGET')
            addresses.add(target['ip'])
    return config['targets']


def pool_snapshot(target, customer, http, *, expected_pool_max=4):
    require(type(expected_pool_max) is int and expected_pool_max in (4, 8)
            and (expected_pool_max == 4 or target['api'] == 'browse'), 'INVALID_EXPECTED_POOL_SIZE')
    status, row = http(target, '/internal/database-pool', customer)
    require(status == 200 and re.fullmatch(r'[0-9a-f]{32}', row['instance_id']) is not None,
            'POOL_PROCESS_UNAVAILABLE')
    values = row['statistics']
    keys = ('pool_min', 'pool_max', 'pool_size', 'pool_available', 'requests_waiting',
            'requests_num', 'connections_num', *ERROR_KEYS)
    require(all(type(values.get(key)) is int and values[key] >= 0 for key in keys), 'INVALID_POOL_COUNTER')
    present = set(REJECTION_KEYS) & set(values)
    require(not present or (present == set(REJECTION_KEYS) and
            all(type(values[key]) is int and values[key] >= 0 for key in REJECTION_KEYS)),
            'INVALID_POOL_REJECTION_COUNTERS')
    waiting_limit = values.get('pool_waiting_limit', 8)
    require(type(waiting_limit) is int and waiting_limit in ((8, 16) if target['api'] == 'browse' else (8,)),
            'POOL_WAITING_LIMIT_CHANGED')
    require(values['pool_min'] in ((8,) if expected_pool_max == 8 else
                                  ((1, 4) if target['api'] == 'browse' else (1,)))
            and values['pool_max'] == expected_pool_max
            and 1 <= values['pool_size'] <= expected_pool_max and values['pool_available'] <= values['pool_size']
            and values['requests_waiting'] <= waiting_limit, 'POOL_BOUNDS_CHANGED')
    return row


def validate_response(api, route, status, body):
    # Classify transport/service status before checking a successful payload.
    expected_status = 404 if api == 'booking' else 200
    require(status == expected_status, 'UNEXPECTED_HTTP_STATUS_' + str(status))
    if api == 'booking':
        require(status == 404 and body == {'detail': {'code': 'RESERVATION_NOT_FOUND'}},
                'BOOKING_LOOKUP_CHANGED')
        return
    require(status == 200 and isinstance(body, dict) and body.get('id') == 'demo-' + route
            and body.get('available') == 100, 'DEMO_INVENTORY_CHANGED')
    if route == 'general':
        require(body.get('inventory_type') == 'GENERAL' and body.get('seats') is None,
                'GENERAL_RESPONSE_CHANGED')
    else:
        seats = body.get('seats')
        require(body.get('inventory_type') == 'SEATED' and isinstance(seats, list)
                and len(seats) == 100 and set(seats) == {f'A{n}' for n in range(1, 101)},
                'SEATED_RESPONSE_CHANGED')


def percentiles(values):
    require(bool(values), 'NO_LATENCY_SAMPLES')
    values = sorted(values)
    return {label: round(values[max(0, math.ceil(len(values) * quantile) - 1)] * 1000, 3)
            for label, quantile in (('p50', .5), ('p95', .95), ('p99', .99), ('max', 1))}


def workload_call(api, target, iteration, customer, missing_id, http):
    route = ('general', 'seated')[(iteration // 2) % 2] if api == 'browse' else 'missing_reservation'
    path = ('/events/demo-' + route + '/availability' if api == 'browse'
            else '/reservations/' + missing_id)
    status, body = http(target, path, customer)
    try:
        validate_response(api, route, status, body)
    except CheckFailed as error:
        error.http_status = status
        raise
    return status, route


def phase(targets, customer, missing_id, workers, http, *, duration=DURATION, period=PERIOD,
          expected_processes=None):
    flat = [target for rows in targets.values() for target in rows]
    initial = {row['pod']: pool_snapshot(row, customer, http) for row in flat}
    if expected_processes is not None:
        require(all(initial[name]['instance_id'] == process for name, process in expected_processes.items()),
                'PROCESS_CHANGED_BETWEEN_PHASES')
    peaks = {row['pod']: {'size': initial[row['pod']]['statistics']['pool_size'],
                         'waiting': initial[row['pod']]['statistics']['requests_waiting']} for row in flat}
    rows = {api: [] for api in targets}
    failure = []
    stop = threading.Event()
    lock = threading.Lock()
    start = time.perf_counter()
    deadline = start + duration
    per_worker_limit = math.ceil(duration / period)

    def observe():
        try:
            while not stop.is_set():
                for target in flat:
                    if stop.is_set():
                        return
                    row = pool_snapshot(target, customer, http)
                    require(row['instance_id'] == initial[target['pod']]['instance_id'], 'PROCESS_CHANGED')
                    values = row['statistics']
                    require(all(values[key] == initial[target['pod']]['statistics'][key] for key in ERROR_KEYS),
                            'POOL_ERROR_INCREASED')
                    peaks[target['pod']]['size'] = max(peaks[target['pod']]['size'], values['pool_size'])
                    peaks[target['pod']]['waiting'] = max(peaks[target['pod']]['waiting'], values['requests_waiting'])
                stop.wait(1)
        except Exception:
            with lock:
                failure.append('POOL_OBSERVER_FAILED')
            stop.set()

    def worker(api, index):
        next_start = start
        for number in range(per_worker_limit):
            if stop.wait(max(0, next_start - time.perf_counter())) or time.perf_counter() >= deadline:
                return
            target = targets[api][(index + number) % 2]
            begun = time.perf_counter()
            status, route, error = None, None, None
            try:
                status, route = workload_call(api, target, number + index * 2, customer, missing_id, http)
            except Exception as problem:
                status = getattr(problem, 'http_status', None)
                error = str(problem) if isinstance(problem, CheckFailed) else type(problem).__name__
                stop.set()  # Stop offering new work on the first unexpected result.
            elapsed = time.perf_counter() - begun
            with lock:
                rows[api].append((target['pod'], elapsed, status, route, error))
            if error:
                return
            # No scheduled-arrival backlog or catch-up bursts if requests are slow.
            next_start = max(begun + period, time.perf_counter())

    with ThreadPoolExecutor(max_workers=workers * 2 + 1) as executor:
        observer = executor.submit(observe)
        futures = [executor.submit(worker, api, index) for api in targets for index in range(workers)]
        try:
            for future in futures:
                future.result(timeout=duration + 10)
            # Include the full offering window in achieved-rate denominators.
            # A pool/HTTP failure still wakes this wait immediately.
            stop.wait(max(0, deadline - time.perf_counter()))
        finally:
            workload_elapsed = time.perf_counter() - start
            stop.set()
            observer.result(timeout=25)
    elapsed = workload_elapsed
    summaries = {}
    for api, attempts in rows.items():
        errors = Counter(row[4] for row in attempts if row[4])
        summaries[api] = {'attempts': len(attempts), 'valid_responses': len(attempts) - sum(errors.values()),
                          'errors': dict(errors), 'status_counts': dict(Counter(str(row[2]) for row in attempts)),
                          'routes': dict(Counter(row[3] for row in attempts if row[3])),
                          'latency_ms': percentiles([row[1] for row in attempts]) if attempts else None,
                          'observed_requests_per_second': round(len(attempts) / elapsed, 3)}
    if failure or any(not row['attempts'] or row['errors'] for row in summaries.values()):
        print(json.dumps({'result': 'READ_LOAD_PHASE_FAILED', 'workers_per_api': workers,
                          'apis': summaries, 'observer_checks': failure}), flush=True)
        raise CheckFailed('WORKLOAD_OR_OBSERVER_FAILED')
    pools = []
    for target in flat:
        after = pool_snapshot(target, customer, http)
        before = initial[target['pod']]
        require(after['instance_id'] == before['instance_id'], 'PROCESS_CHANGED')
        first, last = before['statistics'], after['statistics']
        expected_reads = sum(row[0] == target['pod'] for row in rows[target['api']])
        require(expected_reads > 0 and last['requests_num'] - first['requests_num'] >= expected_reads,
                'BORROW_COUNTS_DO_NOT_MATCH_WORKLOAD')
        require(last['connections_num'] >= first['connections_num']
                and all(last[key] == first[key] for key in ERROR_KEYS), 'POOL_COUNTERS_CHANGED')
        pools.append({'api': target['api'], 'pod': target['pod'],
                      'workload_reads': expected_reads,
                      'pool_borrows_delta': last['requests_num'] - first['requests_num'],
                      'connection_attempts_delta': last['connections_num'] - first['connections_num'],
                      'sampled_peak_size': max(peaks[target['pod']]['size'], last['pool_size']),
                      'sampled_peak_waiting': max(peaks[target['pod']]['waiting'], last['requests_waiting']),
                      'pool_max': 4, 'pool_error_counters_unchanged': True})
    return {'workers_per_api': workers, 'max_workload_http_concurrency': workers * 2,
            'rate_cap_per_api_rps': workers / period, 'scheduled_duration_seconds': duration,
            'elapsed_seconds': round(elapsed, 3), 'apis': summaries, 'pools': pools}


def run(config, http=read_json, *, duration=DURATION, period=PERIOD):
    targets = targets_from(config)
    customer = 'lab-read-load-' + config['run_id']
    # A fresh random UUID with a new simulated customer must return a scoped404.
    from uuid import uuid4
    missing_id = str(uuid4())
    for api, values in targets.items():
        for target in values:
            for iteration in ((0, 2) if api == 'browse' else (0,)):
                workload_call(api, target, iteration, customer, missing_id, http)
    phases = []
    processes = {target['pod']: pool_snapshot(target, customer, http)['instance_id']
                 for values in targets.values() for target in values}
    for workers in (1, 4):
        print(json.dumps({'result': 'READ_LOAD_PHASE_START', 'workers_per_api': workers,
                          'duration_seconds': duration}), flush=True)
        phases.append(phase(targets, customer, missing_id, workers, http, duration=duration, period=period,
                            expected_processes=processes))
    for api, values in targets.items():
        for target in values:
            for iteration in ((0, 2) if api == 'browse' else (0,)):
                workload_call(api, target, iteration, customer, missing_id, http)
    return {'result': 'READ_LOAD_WORKLOAD_OK', 'run_id': config['run_id'], 'phases': phases,
            'inventory_writes': False, 'availability_before_and_after': 100,
            'pool_processes_unchanged': True,
            'scope': 'In-cluster direct-pod reads; Booking is a scoped404 lookup, not a reservation write.',
            'generator_limits': {'cpu_cores': 1, 'memory_mib': 256},
            'latency_slo_tested': False}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.loads(Path('/checks/targets.json').read_text()))), flush=True)
    except Exception as error:
        print(json.dumps({'result': 'READ_LOAD_WORKLOAD_FAILED', 'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, CheckFailed) else None}), flush=True)
        raise SystemExit(1) from None
