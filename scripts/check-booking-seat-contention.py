"""Two customers compete for demo-seated/A1 through distinct Booking pods."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
from uuid import UUID

spec = importlib.util.spec_from_file_location(
    'booking_replica_probe', Path(__file__).with_name('check-booking-concurrency.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
require = probe.require


def experiment(call, customer, key):
    body = {'event_id': 'demo-seated', 'seats': ['A1']}
    identities = [customer + '-seat-' + str(i) for i in range(2)]
    keys = [key + '-seat-' + str(i) for i in range(2)]
    holds = {}
    unresolved = {0, 1}
    gate = threading.Barrier(2)

    def remember(index, status, row):
        if status in (200, 201) and 'id' in row:
            reservation_id = str(UUID(row['id']))
            holds[(index, reservation_id)] = identities[index]
            unresolved.discard(index)
        elif status == 409 and row == {'detail': {'code': 'SEAT_UNAVAILABLE'}}:
            unresolved.discard(index)

    def create(index):
        gate.wait(timeout=10)
        return call(index, 'POST', '/reservations', identities[index], keys[index], body)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, i) for i in range(2)]
            outcomes, failures = [], []
            for index, future in enumerate(futures):
                try:
                    status, row = future.result(timeout=35)
                    remember(index, status, row)
                    outcomes.append((index, status, row))
                except Exception as error:
                    failures.append(error)
        if failures:
            raise failures[0]
        require(sorted(s for _, s, _ in outcomes) == [201, 409],
                'Expected one winning hold and one conflict')
        require(len(holds) == 1, 'Expected exactly one winning reservation')
        winner, status, held = next(o for o in outcomes if o[1] == 201)
        loser, status, rejected = next(o for o in outcomes if o[1] == 409)
        require(rejected == {'detail': {'code': 'SEAT_UNAVAILABLE'}},
                'Unexpected conflict response')
        require(held['status'] == 'HELD' and held['event_id'] == 'demo-seated'
                and held['quantity'] == 1 and held['seats'] == ['A1'], 'Unexpected seat hold')
        for index in range(2):
            status, row = call(index, 'GET', '/reservations/' + held['id'], identities[winner])
            require(status == 200 and row == held, 'Replicas disagree on winning hold')
        return {'result': 'BOOKING_SEAT_CONTENTION_OK', 'event_id': 'demo-seated',
                'seat': 'A1', 'http_statuses': [201, 409],
                'conflict_code': 'SEAT_UNAVAILABLE', 'reservation_id': held['id'],
                'winning_pod_index': winner, 'losing_pod_index': loser,
                'both_replica_reads': True, 'final_status': 'CANCELLED'}
    finally:
        # Resolve every ambiguous create before releasing the winner's seat.
        # Otherwise a cleanup replay for the loser could acquire the freed seat.
        for index in sorted(unresolved.copy()):
            try:
                status, row = call(index, 'POST', '/reservations', identities[index], keys[index], body)
                remember(index, status, row)
            except Exception:
                pass
        failed = []
        for (owner_index, reservation_id), identity in holds.items():
            try:
                status, row = call(owner_index, 'POST', '/reservations/' + reservation_id + '/cancel', identity)
                require(status == 200 and row.get('status') == 'CANCELLED', 'Cancellation failed')
                for index in range(2):
                    status, row = call(index, 'GET', '/reservations/' + reservation_id, identity)
                    require(status == 200 and row.get('status') == 'CANCELLED', 'Readback failed')
            except Exception:
                failed.append(reservation_id)
        require(not unresolved and not failed,
                'Unresolved request or cancellation; inspect each customer/key with -seat-0/-seat-1 suffix')


if __name__ == '__main__':
    raise SystemExit(probe.main(experiment_fn=experiment,
                               failure_result='BOOKING_SEAT_CONTENTION_FAILED',
                               recovery_note='Append -seat-0 or -seat-1 to BOTH printed customer '
                                             'and idempotency_key to inspect each original request. '
                                             'An uncancelled hold expires logically after five '
                                             'minutes; do not reset inventory or use a new key.'))
