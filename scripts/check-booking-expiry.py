"""User-run real five-minute expiry and assigned-seat reuse experiment."""
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import time
from uuid import UUID

spec = importlib.util.spec_from_file_location(
    'booking_expiry_transport', Path(__file__).with_name('check-booking-concurrency.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
require = probe.require


def experiment(call, customer, key):
    body = {'event_id': 'demo-seated', 'seats': ['A1']}
    identities = [customer + '-expiry-' + str(i) for i in range(2)]
    keys = [key + '-expiry-' + str(i) for i in range(2)]
    ids = {}
    known = set()
    uncertain = set()
    completed = False

    def create(index):
        uncertain.add(index)
        status, row = call(index, 'POST', '/reservations', identities[index], keys[index], body)
        if status in (200, 201) and 'id' in row:
            reservation_id = str(UUID(row['id']))
            known.add((index, reservation_id))
            ids.setdefault(index, reservation_id)
            uncertain.discard(index)
        elif status == 409 and row == {'detail': {'code': 'SEAT_UNAVAILABLE'}}:
            uncertain.discard(index)
        return status, row

    try:
        status, held = create(0)
        require(status == 201 and held['status'] == 'HELD'
                and held['event_id'] == 'demo-seated' and held['seats'] == ['A1']
                and held['quantity'] == 1, 'Unexpected initial hold')
        created = datetime.fromisoformat(held['created_at'].replace('Z', '+00:00'))
        expiry = datetime.fromisoformat(held['expires_at'].replace('Z', '+00:00'))
        require(created.tzinfo is not None and expiry.tzinfo is not None
                and 290 <= (expiry - created).total_seconds() <= 310,
                'Expected the configured five-minute hold')
        status, rejected = create(1)
        require(status == 409 and rejected == {'detail': {'code': 'SEAT_UNAVAILABLE'}},
                'Seat was not protected during the hold')
        print(json.dumps({'result': 'WAITING_FOR_HOLD_EXPIRY', 'seat': 'A1',
                          'reservation_id': ids[0], 'wait_seconds': 305,
                          'note': 'Keep this Cloud Shell command running.'}), flush=True)
        # Wait a full hold duration after its response, avoiding dependence on
        # the Cloud Shell wall clock. No SQL timestamp changes or TTL override.
        deadline = time.monotonic() + 305
        while time.monotonic() < deadline:
            time.sleep(min(10, max(0, deadline - time.monotonic())))
        # The rival's new booking transaction performs lazy expiry and releases
        # the old assignment. This is not evidence of an expiry worker running.
        status, replacement = create(1)
        require(status == 201 and replacement['status'] == 'HELD'
                and replacement['seats'] == ['A1'] and replacement['quantity'] == 1
                and replacement['event_id'] == 'demo-seated'
                and ids[1] != ids[0], 'Expired seat was not reusable')
        for index in range(2):
            status, old = call(index, 'GET', '/reservations/' + ids[0], identities[0])
            require(status == 200 and old['status'] == 'EXPIRED', 'Old hold is not EXPIRED')
        status, replay = create(0)
        require(status == 200 and replay['id'] == ids[0] and replay['status'] == 'EXPIRED',
                'Old key created a fresh hold')
        completed = True
        return {'result': 'BOOKING_EXPIRY_OK', 'seat': 'A1',
                'expired_reservation_id': ids[0], 'replacement_reservation_id': ids[1],
                'blocked_before_expiry': True, 'seat_rebooked_after_expiry': True,
                'expired_key_reused_result': True, 'both_replica_reads': True,
                'replacement_final_status': 'CANCELLED', 'expiry_mechanism': 'booking_transaction'}
    finally:
        # Recover ambiguous creates using their original keys before cleanup.
        for index in sorted(uncertain.copy()):
            try:
                create(index)
            except Exception:
                pass
        failures = []
        for owner, reservation_id in known:
            try:
                status, row = call(owner, 'GET', '/reservations/' + reservation_id, identities[owner])
                require(status == 200, 'Cleanup lookup failed')
                if row['status'] == 'HELD':
                    status, row = call(owner, 'POST', '/reservations/' + reservation_id + '/cancel',
                                       identities[owner])
                    require(status == 200 and row['status'] == 'CANCELLED', 'Cancellation failed')
                require(row['status'] in ('EXPIRED', 'CANCELLED'), 'Unexpected final state')
                if completed and owner == 1:
                    require(row['status'] == 'CANCELLED', 'Replacement cancellation not verified')
                for index in range(2):
                    status, readback = call(index, 'GET', '/reservations/' + reservation_id,
                                            identities[owner])
                    require(status == 200 and readback['status'] == row['status'],
                            'Cleanup readback disagrees')
            except Exception:
                failures.append(reservation_id)
        require(not uncertain and not failures, 'Unresolved request or hold cleanup failure')


if __name__ == '__main__':
    raise SystemExit(probe.main(experiment_fn=experiment, failure_result='BOOKING_EXPIRY_FAILED',
                               recovery_note='Append -expiry-0 or -expiry-1 to BOTH customer and '
                                             'idempotency_key to inspect each original request. '
                                             'Do not reset inventory or retry using a new key.'))
