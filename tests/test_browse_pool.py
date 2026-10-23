"""Real PostgreSQL reuse/rollback/replacement tests in the dedicated CI database."""
from concurrent.futures import ThreadPoolExecutor

import pytest
import psycopg
from fastapi import HTTPException

from trafficxx.browse_db import BrowseDatabase


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv('BROWSE_DB_POOL_MAX', '1')
    database = BrowseDatabase()
    database.open()
    try:
        database.pool.wait(timeout=15)
        yield database
    finally:
        database.close()


def test_sequential_requests_reuse_physical_connection(database):
    with database.connection() as conn:
        first = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
    with database.connection() as conn:
        assert conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] == first


def test_failed_transaction_rolls_back_before_reuse(database):
    with pytest.raises(psycopg.errors.DivisionByZero):
        with database.connection() as conn:
            conn.execute("INSERT INTO events VALUES ('pool-rollback', 'Must roll back', 'GENERAL')")
            conn.execute('SELECT 1 / 0')
    with database.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM events WHERE id = 'pool-rollback'").fetchone()['n'] == 0


def test_capacity_wait_times_out_without_opening_extra_connection(database):
    def borrow():
        with pytest.raises(HTTPException) as caught:
            with database.connection():
                pytest.fail('Pool must remain limited to one connection')
        assert caught.value.status_code == 503

    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.connection():
            executor.submit(borrow).result(timeout=10)
        with database.connection() as conn:
            assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
        assert database.pool.get_stats()['pool_size'] == 1


def test_closed_connection_is_replaced(database):
    with database.connection() as conn:
        first = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        conn.close()
    with database.connection() as conn:
        assert conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] != first


def test_serving_api_statistics_track_the_same_pool_as_reads():
    from fastapi.testclient import TestClient
    from trafficxx.browse import app

    with TestClient(app) as client:
        before = client.get('/internal/database-pool').json()
        assert client.get('/events').status_code == 200
        assert client.get('/readyz').status_code == 200
        after = client.get('/internal/database-pool').json()
        assert before['instance_id'] == after['instance_id']
        assert after['statistics']['requests_num'] >= before['statistics']['requests_num'] + 2
        assert after['statistics']['pool_max'] == 4
        assert 'password' not in str(after)
        response = client.get('/metrics')
        assert response.status_code == 200
        assert '# TYPE trafficxx_browse_pool_borrows_total counter' in response.text
        assert client.get('/internal/database-pool').json()['statistics']['requests_num'] == after['statistics']['requests_num']
