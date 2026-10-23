"""Booking transactions through a real pool, in the disposable CI database only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trafficxx.booking_db import BookingDatabase
from trafficxx.db import connect
from trafficxx.models import ReservationRequest
from trafficxx.service import reservation, reserve


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv('BOOKING_DB_POOL_MAX', '1')
    pool = BookingDatabase()
    pool.open()
    try:
        pool.pool.wait(timeout=15)
        yield pool
    finally:
        pool.close()


def pid(database):
    with database.connection() as conn:
        return conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']


def test_outbox_failure_rolls_back_all_writes_and_releases_transaction_locks(database, monkeypatch):
    before = pid(database)
    customer, key = 'pool-customer', uuid4().hex

    def reject_emit(*args):
        raise RuntimeError('Injected outbox failure')

    with monkeypatch.context() as patch:
        patch.setattr('trafficxx.service.emit', reject_emit)
        with pytest.raises(RuntimeError, match='Injected outbox failure'):
            reserve(customer, key, ReservationRequest(event_id='general', quantity=2),
                    connection_factory=database.connection)
    # Same physical session is usable after rollback; no open transaction locks.
    assert pid(database) == before
    with connect() as observer:
        assert observer.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 0
        assert observer.execute('SELECT count(*) AS n FROM outbox').fetchone()['n'] == 0
        assert observer.execute('SELECT count(*) AS n FROM ticket_units '
                                'WHERE reservation_id IS NOT NULL').fetchone()['n'] == 0
        assert observer.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS ok',
                                (f'{len(customer)}:{customer}{key}',)).fetchone()['ok']
        observer.execute("SELECT id FROM events WHERE id = 'general' FOR UPDATE NOWAIT")
    row, created = reserve(customer, key, ReservationRequest(event_id='general', quantity=2),
                           connection_factory=database.connection)
    assert created and row['status'] == 'HELD'
    assert pid(database) == before


def test_expiry_conflict_commits_cleanup_before_returning_connection(database):
    before = pid(database)
    row, _ = reserve('alice', uuid4().hex, ReservationRequest(event_id='seated', seats=['A1']),
                     connection_factory=database.connection)
    with connect() as conn:
        conn.execute("UPDATE reservations SET expires_at = clock_timestamp() - interval '1 second' "
                     'WHERE id = %s', (row['id'],))
    with pytest.raises(HTTPException) as error:
        reservation('alice', row['id'], 'CONFIRMED', connection_factory=database.connection)
    assert error.value.detail == {'code': 'RESERVATION_EXPIRED'}
    assert pid(database) == before
    with connect() as conn:
        assert conn.execute('SELECT status FROM reservations WHERE id = %s',
                            (row['id'],)).fetchone()['status'] == 'EXPIRED'
        assert conn.execute('SELECT count(*) AS n FROM ticket_units WHERE reservation_id = %s',
                            (row['id'],)).fetchone()['n'] == 0
        assert conn.execute("SELECT count(*) AS n FROM outbox WHERE reservation_id = %s "
                            "AND event_type = 'EXPIRED'", (row['id'],)).fetchone()['n'] == 1


def test_pooled_concurrent_duplicates_keep_one_reservation_and_outbox(monkeypatch):
    monkeypatch.setenv('BOOKING_DB_POOL_MAX', '2')
    database = BookingDatabase()
    database.open()
    try:
        database.pool.wait(timeout=15)
        key = uuid4().hex
        body = ReservationRequest(event_id='general', quantity=2)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: reserve('alice', key, body, connection_factory=database.connection), range(8)))
        assert sum(created for _, created in results) == 1
        assert len({row['id'] for row, _ in results}) == 1
        assert len({row['expires_at'] for row, _ in results}) == 1
        with connect() as conn:
            assert conn.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 1
            assert conn.execute('SELECT count(*) AS n FROM outbox').fetchone()['n'] == 1
        assert database.statistics()['statistics']['pool_size'] <= 2
    finally:
        database.close()


def test_pooled_same_seat_contention_keeps_one_winner(monkeypatch):
    monkeypatch.setenv('BOOKING_DB_POOL_MAX', '2')
    database = BookingDatabase()
    database.open()
    barrier = Barrier(2)

    def attempt(customer):
        barrier.wait(timeout=5)
        try:
            reserve(customer, uuid4().hex, ReservationRequest(event_id='seated', seats=['A1']),
                    connection_factory=database.connection)
            return 201
        except HTTPException as error:
            assert error.detail == {'code': 'SEAT_UNAVAILABLE'}
            return error.status_code

    try:
        database.pool.wait(timeout=15)
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(attempt, ('alice', 'bob'))) == [201, 409]
        with connect() as conn:
            assert conn.execute('SELECT count(*) AS n FROM reservations').fetchone()['n'] == 1
            assert conn.execute('SELECT count(*) AS n FROM outbox').fetchone()['n'] == 1
    finally:
        database.close()


def test_http_requests_and_readiness_share_booking_pool(monkeypatch):
    from trafficxx.booking import app, database

    monkeypatch.setenv('BOOKING_DB_POOL_MAX', '1')
    with TestClient(app) as client:
        before = client.get('/internal/database-pool').json()
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 200
        headers = {'X-Customer-Id': 'alice', 'Idempotency-Key': uuid4().hex}
        body = {'event_id': 'general', 'quantity': 2}
        first = client.post('/reservations', headers=headers, json=body)
        assert first.status_code == 201
        path = '/reservations/' + first.json()['id']
        assert client.post('/reservations', headers=headers, json=body).json() == first.json()
        assert client.get(path, headers=headers).status_code == 200
        assert client.post(path + '/cancel', headers=headers).json()['status'] == 'CANCELLED'
        after = client.get('/internal/database-pool').json()
        assert before['instance_id'] == after['instance_id']
        assert after['statistics']['requests_num'] == before['statistics']['requests_num'] + 5
        assert after['statistics']['pool_max'] == 1
        # No pool slot is available; an HTTP request must receive a bounded 503.
        with database.connection():
            rejected = client.get(path, headers=headers)
            assert rejected.status_code == 503
            assert rejected.json() == {'detail': {'code': 'DATABASE_BUSY'}}
        assert client.get('/readyz').status_code == 200
    assert database.pool is None
