from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trafficxx.booking import app
from trafficxx.browse import app as browse_app
from trafficxx.db import connect
from trafficxx.models import ReservationRequest
from trafficxx.service import reservation, reserve, sweep


def create(customer='alice', key=None, **body):
    return reserve(customer, key or str(uuid4()), ReservationRequest(**body))[0]


def past_deadline(reservation_id):
    with connect() as conn:
        conn.execute("UPDATE reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                     (reservation_id,))


def test_http_flow_and_ownership():
    with TestClient(app) as booking, TestClient(browse_app) as browse:
        assert booking.get('/healthz').status_code == 200
        assert booking.get('/readyz').status_code == 200
        assert len(browse.get('/events').json()) == 2
        headers = {'X-Customer-Id': 'alice', 'Idempotency-Key': 'purchase-1'}
        body = {'event_id': 'seated', 'seats': ['A2', 'A1']}
        first = booking.post('/reservations', json=body, headers=headers)
        assert first.status_code == 201
        row = first.json()
        assert (browse.get('/events/seated/availability').json()['available']) == 98
        retry = booking.post('/reservations', json={**body, 'seats': ['A1', 'A2']}, headers=headers)
        assert retry.status_code == 200
        assert retry.json() == row
        path = '/reservations/' + row['id']
        assert booking.get(path, headers={'X-Customer-Id': 'bob'}).status_code == 404
        assert booking.post(path + '/confirm', headers=headers).json()['status'] == 'CONFIRMED'
        assert booking.post(path + '/confirm', headers=headers).status_code == 200
        assert booking.post(path + '/cancel', headers=headers).status_code == 409


def test_group_rejection_does_not_claim_other_seats():
    create(event_id='seated', seats=['A2'])
    with pytest.raises(HTTPException) as error:
        create(event_id='seated', seats=['A1', 'A2'])
    assert error.value.detail['code'] == 'SEAT_UNAVAILABLE'
    assert create(event_id='seated', seats=['A1'])['status'] == 'HELD'


def test_outbox_failure_rolls_back_inventory_and_reservation(monkeypatch):
    def crash_before_outbox_commit(*args):
        raise RuntimeError('Simulated crash before commit')
    with monkeypatch.context() as patch:
        patch.setattr('trafficxx.service.emit', crash_before_outbox_commit)
        with pytest.raises(RuntimeError, match='Simulated crash'):
            create(event_id='general', quantity=100)
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM outbox').fetchone()['n'] == 0
    assert create(event_id='general', quantity=100)


def test_key_reuse_with_different_payload_is_rejected():
    create(key='same', event_id='general', quantity=1)
    with pytest.raises(HTTPException) as error:
        create(key='same', event_id='general', quantity=2)
    assert error.value.detail['code'] == 'IDEMPOTENCY_KEY_REUSED'
    assert create(customer='bob', key='same', event_id='general', quantity=1)


def test_concurrent_duplicate_creates_one_reservation_and_outbox_event():
    with ThreadPoolExecutor(max_workers=16) as pool:
        rows = list(pool.map(lambda _: create(key='one-purchase', event_id='general', quantity=2), range(40)))
    assert len({row['id'] for row in rows}) == 1
    assert len({row['expires_at'] for row in rows}) == 1
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM outbox').fetchone()['n'] == 1


def test_1000_attempts_cannot_oversell_100_tickets():
    def attempt(n):
        try:
            create(customer=f'customer-{n}', event_id='general', quantity=1)
            return True
        except HTTPException as error:
            assert error.status_code == 409
            assert error.detail['code'] == 'INSUFFICIENT_INVENTORY'
            return False

    # 1,000 total attempts at bounded concurrency; not a 1,000-client benchmark.
    with ThreadPoolExecutor(max_workers=32) as pool:
        assert sum(pool.map(attempt, range(1000))) == 100
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 100
        assert conn.execute('SELECT count(*) AS n FROM ticket_units WHERE reservation_id IS NOT NULL').fetchone()['n'] == 100


def test_concurrent_claims_for_the_same_seat_have_one_winner():
    def attempt(n):
        try:
            create(customer=f'customer-{n}', event_id='seated', seats=['A1', 'A2'])
            return True
        except HTTPException as error:
            assert error.detail['code'] == 'SEAT_UNAVAILABLE'
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(attempt, range(40))) == 1


def test_expiry_releases_once_and_late_confirmation_fails():
    row = create(event_id='seated', seats=['A1'])
    past_deadline(row['id'])
    with pytest.raises(HTTPException) as error:
        reservation('alice', row['id'], 'CONFIRMED')
    assert error.value.detail['code'] == 'RESERVATION_EXPIRED'
    assert sweep() == 0  # The conflict response committed expiry cleanup.
    assert create(event_id='seated', seats=['A1'])
    assert reservation('alice', row['id'])['status'] == 'EXPIRED'
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox WHERE event_type = 'EXPIRED'").fetchone()['n'] == 1


def test_cancel_retry_releases_once():
    row = create(event_id='general', quantity=100)
    assert reservation('alice', row['id'], 'CANCELLED')['status'] == 'CANCELLED'
    assert reservation('alice', row['id'], 'CANCELLED')['status'] == 'CANCELLED'
    assert create(event_id='general', quantity=100)
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox WHERE event_type = 'CANCELLED'").fetchone()['n'] == 1


def test_expired_hold_can_be_reclaimed_without_waiting_for_worker():
    row = create(event_id='general', quantity=100)
    past_deadline(row['id'])
    assert create(customer='bob', event_id='general', quantity=100)
    assert reservation('alice', row['id'])['status'] == 'EXPIRED'


def test_confirmation_and_cancellation_race():
    row = create(event_id='general', quantity=100)
    def transition(action):
        try:
            return reservation('alice', row['id'], action)['status']
        except HTTPException as error:
            assert error.status_code == 409
            return 'CONFLICT'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(transition, ['CONFIRMED', 'CANCELLED']))
    assert results.count('CONFLICT') == 1
    final = reservation('alice', row['id'])['status']
    with connect() as conn:
        claimed = conn.execute('SELECT count(*) AS n FROM ticket_units WHERE reservation_id IS NOT NULL').fetchone()['n']
        assert claimed == (100 if final == 'CONFIRMED' else 0)


def test_expiry_and_confirmation_race():
    row = create(event_id='general', quantity=100)
    past_deadline(row['id'])
    def confirm():
        with pytest.raises(HTTPException):
            reservation('alice', row['id'], 'CONFIRMED')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm), pool.submit(sweep)]
        for future in futures:
            future.result()
    assert reservation('alice', row['id'])['status'] == 'EXPIRED'
    assert create(event_id='general', quantity=100)


def test_confirmation_uses_time_after_waiting_for_inventory_lock():
    row = create(event_id='general', quantity=1)
    with connect() as conn:
        conn.execute("UPDATE reservations SET expires_at = clock_timestamp() + interval '0.5 seconds' WHERE id = %s",
                     (row['id'],))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with connect() as conn:
            conn.execute("SELECT * FROM events WHERE id = 'general' FOR UPDATE")
            pending = pool.submit(reservation, 'alice', row['id'], 'CONFIRMED')
            conn.execute('SELECT pg_sleep(0.75)')
        with pytest.raises(HTTPException) as error:
            pending.result()
        assert error.value.detail['code'] == 'RESERVATION_EXPIRED'
    finally:
        pool.shutdown()


@pytest.mark.parametrize('body', [
    {'event_id': 'general', 'quantity': 0},
    {'event_id': 'general', 'quantity': True},
    {'event_id': 'seated', 'seats': ['A1', 'A1']},
    {'event_id': 'seated', 'seats': []},
    {'event_id': 'general', 'quantity': 1, 'seats': ['A1']},
    {'event_id': 'general'},
])
def test_invalid_requests(body):
    with TestClient(app) as client:
        response = client.post('/reservations', json=body,
                               headers={'X-Customer-Id': 'alice', 'Idempotency-Key': 'invalid'})
        assert response.status_code == 422
