"""Single-statement availability semantics in the dedicated trafficxx_test DB."""
from uuid import uuid4

import pytest

from trafficxx.browse_queries import fetch_availability
from trafficxx.db import connect


@pytest.mark.parametrize('event_id', ['general', 'seated'])
def test_single_statement_preserves_full_inventory_and_order(event_id):
    with connect() as conn:
        row = fetch_availability(conn, event_id)
    assert row['available'] == 100
    assert row['seats'] == ([f'A{n}' for n in range(1, 101)] if event_id == 'seated' else None)
    assert '_available_seats' not in row


def test_empty_inventory_is_an_event_and_missing_event_is_none():
    with connect() as conn:
        conn.execute("INSERT INTO events VALUES ('empty', 'Empty', 'SEATED')")
        row = fetch_availability(conn, 'empty')
        assert row['available'] == 0 and row['seats'] == []
        assert fetch_availability(conn, 'missing') is None


def test_expired_holds_logically_release_units_without_writes():
    states = [('HELD', '-1 hour'), ('HELD', '1 hour'), ('CONFIRMED', '-1 hour'),
              ('CANCELLED', '-1 hour'), ('EXPIRED', '-1 hour')]
    with connect() as conn:
        for unit, (status, expiry) in enumerate(states, 1):
            reservation = uuid4()
            conn.execute("INSERT INTO reservations(id,event_id,customer_id,idempotency_key,request,quantity,status,expires_at) "
                         "VALUES (%s,'seated','query-test',%s,'{}',1,%s,clock_timestamp() + %s::interval)",
                         (reservation, str(reservation), status, expiry))
            conn.execute("UPDATE ticket_units SET reservation_id=%s WHERE event_id='seated' AND unit_number=%s",
                         (reservation, unit))
        row = fetch_availability(conn, 'seated')
        # Match the existing predicate, including assigned cancelled/expired rows:
        # only unassigned units or assigned HELD reservations past expiry are free.
        assert row['available'] == 96
        assert row['seats'] == ['A1'] + [f'A{n}' for n in range(6, 101)]
        assert conn.execute("SELECT count(*) AS n FROM ticket_units WHERE reservation_id IS NOT NULL").fetchone()['n'] == 5


def test_http_contract_and_combined_timer():
    from fastapi.testclient import TestClient
    from trafficxx.browse import app
    with TestClient(app) as client:
        before = client.get('/internal/database-pool').json()['statistics']
        reply = client.get('/events/seated/availability')
        assert reply.status_code == 200
        assert reply.json()['available'] == 100
        assert reply.json()['seats'] == [f'A{n}' for n in range(1, 101)]
        missing = client.get('/events/not-there/availability')
        assert missing.status_code == 404
        assert missing.json() == {'detail': {'code': 'EVENT_NOT_FOUND'}}
        after = client.get('/internal/database-pool').json()['statistics']
        assert after['timing_availability_query_count'] - before['timing_availability_query_count'] == 2
        assert after['timing_event_query_count'] == before['timing_event_query_count']
        assert after['timing_units_query_count'] == before['timing_units_query_count']
