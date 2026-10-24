"""Cloud Shell write experiment: 18 create/cancel pairs, at most four flows."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent
BODY = {'event_id': 'demo-general', 'quantity': 1}


class CheckFailed(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Journal:
    """Persist lab recovery identifiers before each POST; contains no Azure secrets."""
    def __init__(self, path, customer, key):
        self.path = path
        self.lock = threading.Lock()
        self.rows = [{'number': number, 'customer': customer + '-' + str(number),
                      'key': key + '-' + str(number), 'attempted': False,
                      'id': None, 'cleanup_verified': False} for number in range(18)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'body': BODY, 'intents': self.rows}, stream)

    def save(self, row, **values):
        with self.lock:
            row.update(values)
            temporary = self.path.with_suffix('.tmp')
            descriptor = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                json.dump({'body': BODY, 'intents': self.rows}, stream)
            os.replace(temporary, self.path)


def cancel_and_verify(call, journal, row, primary):
    customer, key = row['customer'], row['key']
    if row['id'] is None:
        # A lost create response is ambiguous. Recover with the SAME customer,
        # key and body once; this can create one hold if the original never did.
        status, result = call(1 - primary, 'POST', '/reservations', customer, key, BODY)
        require(status in (200, 201), 'CREATE_OUTCOME_UNRESOLVED')
        journal.save(row, id=str(UUID(result['id'])))
    path = '/reservations/' + row['id']
    failure = None
    for attempt in range(2):  # Cancellation is idempotent; at most one retry.
        try:
            status, result = call(1 - primary, 'POST', path + '/cancel', customer)
            require(status == 200 and result.get('id') == row['id']
                    and result.get('status') == 'CANCELLED', 'CANCEL_NOT_VERIFIED')
            for index in (0, 1):
                status, result = call(index, 'GET', path, customer)
                require(status == 200 and result.get('id') == row['id']
                        and result.get('status') == 'CANCELLED', 'CANCEL_READBACK_FAILED')
            journal.save(row, cleanup_verified=True)
            return
        except Exception as error:
            failure = error
    raise CheckFailed('CLEANUP_INCOMPLETE') from failure


def flow(call, journal, row, clock=time.perf_counter):
    primary = row['number'] % 2
    journal.save(row, attempted=True)
    reserve_ms = cancel_ms = None
    try:
        started = clock()
        status, result = call(primary, 'POST', '/reservations', row['customer'], row['key'], BODY)
        reserve_ms = (clock() - started) * 1000
        if isinstance(result, dict) and 'id' in result:
            journal.save(row, id=str(UUID(result['id'])))
        require(status == 201 and result.get('status') == 'HELD'
                and result.get('event_id') == 'demo-general' and result.get('quantity') == 1
                and row['id'] is not None, 'HOLD_RESPONSE_INVALID')
        started = clock()
        status, result = call(1 - primary, 'POST', '/reservations/' + row['id'] + '/cancel', row['customer'])
        require(status == 200 and result.get('id') == row['id']
                and result.get('status') == 'CANCELLED', 'CANCEL_RESPONSE_INVALID')
        cancel_ms = (clock() - started) * 1000
    finally:
        # Also read cancellation from both replicas. Recovery calls are excluded
        # from timing samples, and never convert a failed workload into a pass.
        cancel_and_verify(call, journal, row, primary)
    return {'reserve_ms': reserve_ms, 'cancel_ms': cancel_ms}


def experiment(call, customer, key, journal_path):
    journal = Journal(journal_path, customer, key)
    timing = load('check-read-load')
    phases, offset = [], 0
    for workers, count in ((1, 6), (4, 12)):
        stop = threading.Event()

        def work(rows):
            outcomes = []
            for row in rows:
                if stop.is_set():
                    break
                started = time.monotonic()
                try:
                    outcomes.append(flow(call, journal, row))
                except BaseException:
                    stop.set()
                    raise
                # At most one new flow per worker per second; no catch-up.
                delay = 1 - (time.monotonic() - started)
                if delay > 0:
                    stop.wait(delay)
            return outcomes
        print(json.dumps({'result': 'BOOKING_WRITE_PHASE_STARTED', 'workers': workers,
                          'planned_pairs': count, 'journal': str(journal_path)}), flush=True)
        started = time.monotonic()
        rows = journal.rows[offset:offset + count]
        outcomes, failure = [], None
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(work, rows[index::workers]) for index in range(workers)]
            for future in futures:
                try:
                    outcomes.extend(future.result())
                except BaseException as error:
                    stop.set()
                    failure = error
        if failure is not None:
            raise failure
        require(len(outcomes) == count and all(row['cleanup_verified'] for row in rows), 'PHASE_INCOMPLETE')
        phases.append({'workers': workers, 'completed_pairs': count,
                       'elapsed_seconds': round(time.monotonic() - started, 3),
                       'reserve_latency_ms': timing.percentiles([row['reserve_ms'] / 1000 for row in outcomes]),
                       'cancel_latency_ms': timing.percentiles([row['cancel_ms'] / 1000 for row in outcomes])})
        offset += count
    require(len({row['id'] for row in journal.rows}) == 18, 'DUPLICATE_RESERVATION_IDS')
    return {'result': 'BOOKING_WRITE_WORKLOAD_OK', 'phases': phases, 'created_and_cancelled': 18,
            'both_replica_cancel_readbacks': True, 'cleanup_verified': True,
            'journal': str(journal_path), 'inventory_writes': True,
            'scope': 'Cloud Shell pod port-forwards; timings include client/network overhead.',
            'latency_slo_tested': False}


def availability():
    output = subprocess.check_output(['bash', str(ROOT / 'check-browse-http.sh'), '--demo'],
                                     text=True, timeout=180, stderr=subprocess.DEVNULL)
    result = json.loads(output.splitlines()[-1])
    require(result.get('result') == 'BROWSE_DEMO_HTTP_OK'
            and result.get('demo_availability') == {'demo-general': 100, 'demo-seated': 100},
            'DEMO_INVENTORY_NOT_FULL')
    return result['demo_availability']


def main():
    journal_path = Path.home() / ('trafficxx-booking-write-' + uuid4().hex[:12] + '.json')
    try:
        sub = subprocess.check_output(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'],
                                      text=True, timeout=30, stderr=subprocess.DEVNULL).strip()
        require(sub == '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85', 'WRONG_SUBSCRIPTION')
        runner = load('run-read-load')
        guard = load('check-browse-concurrency')
        require(runner.kube('config', 'current-context') == 'trafficxx-lab-aks', 'WRONG_CONTEXT')
        before = {api: runner.snapshot(guard, api) for api in ('browse', 'booking')}
        require(availability() == {'demo-general': 100, 'demo-seated': 100}, 'INVENTORY_BASELINE_FAILED')
        workload = []

        def execute(call, customer, key):
            result = experiment(call, customer, key, journal_path)
            workload.append(result)
            return result
        base = load('check-booking-concurrency')
        code = base.main(experiment_fn=execute, failure_result='BOOKING_WRITE_FAILED',
                         recovery_note='Inspect the write journal; do not retry using new keys. No SLO claim.')
        require(code == 0 and len(workload) == 1, 'WRITE_WORKLOAD_FAILED')
        after_inventory = availability()
        after = {api: runner.snapshot(guard, api) for api in ('browse', 'booking')}
        require(before == after, 'SERVING_PODS_CHANGED')
        print(json.dumps({'result': 'BOOKING_WRITE_BASELINE_OK', 'created_and_cancelled': 18,
                          'availability_after': after_inventory, 'serving_pods_unchanged': True,
                          'serving_images': {api: sorted({row['image'] for row in state['pods']})
                                             for api, state in before.items()},
                          'journal': str(journal_path), 'inventory_writes': True,
                          'note': 'Use preceding workload timings; not an in-cluster capacity or latency SLO test.'}))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'result': 'BOOKING_WRITE_BASELINE_FAILED', 'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, CheckFailed) else None,
                          'journal': str(journal_path) if journal_path.exists() else None,
                          'note': 'Keep any journal and inspect attempted entries whose cleanup_verified is false. '
                                  'Use their original customer/key/body; do not reset inventory or rerun with new keys. '
                                  'Uncancelled holds expire after five minutes; hard interruption may prevent cleanup.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
