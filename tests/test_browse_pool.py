"""Real PostgreSQL reuse/rollback/replacement tests in the dedicated CI database."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
import psycopg
from fastapi import HTTPException

from trafficxx.browse_db import BrowseDatabase


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv('BROWSE_DB_POOL_MAX', '1')
    monkeypatch.setenv('BROWSE_DB_CHECK_INTERVAL_SECONDS', '0')
    database = BrowseDatabase()
    database.open()
    try:
        database.pool.wait(timeout=15)
        yield database
    finally:
        database.close()


def test_sequential_requests_reuse_physical_connection(database):
    before = database.statistics()['statistics']['timing_health_check_count']
    with database.connection() as conn:
        first = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
    with database.connection() as conn:
        assert conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] == first
    stats = database.statistics()['statistics']
    assert stats['timing_health_check_count'] == before + 2
    assert stats['timing_health_check_total_us'] >= 0


def test_single_statement_read_returns_idle_without_an_explicit_transaction(database):
    from psycopg.pq import TransactionStatus
    with database.read_connection() as conn:
        assert conn.autocommit is True
        first = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        assert conn.info.transaction_status == TransactionStatus.IDLE
    with pytest.raises(psycopg.errors.DivisionByZero):
        with database.read_connection() as conn:
            conn.execute('SELECT 1 / 0')
    with database.read_connection() as conn:
        assert conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] == first
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_explicit_transaction_path_still_rolls_back_an_autocommit_pool(database):
    from psycopg.pq import TransactionStatus
    with pytest.raises(ValueError):
        with database.connection() as conn:
            assert conn.info.transaction_status == TransactionStatus.INTRANS
            conn.execute("INSERT INTO events VALUES ('explicit-rollback', 'Must roll back', 'GENERAL')")
            raise ValueError('abort request')
    with database.read_connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM events WHERE id='explicit-rollback'").fetchone()['n'] == 0


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


def test_recent_checks_expire_and_sql_errors_invalidate_success(monkeypatch):
    monkeypatch.setenv('BROWSE_DB_POOL_MAX', '1')
    monkeypatch.setenv('BROWSE_DB_CHECK_INTERVAL_SECONDS', '1')
    database = BrowseDatabase()
    database.open()
    try:
        with patch('trafficxx.database_pool.monotonic', return_value=0) as clock:
            for _ in range(3):
                with database.read_connection() as conn:
                    assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
            stats = database.statistics()['statistics']
            assert stats['timing_health_check_count'] == 1
            assert stats['health_checks_skipped'] == 2
            clock.return_value = 1
            with pytest.raises(psycopg.errors.DivisionByZero):
                with database.read_connection() as conn:
                    conn.execute('SELECT 1 / 0')
            with database.read_connection() as conn:
                assert conn.execute('SELECT 1 AS n').fetchone()['n'] == 1
            assert database.statistics()['statistics']['timing_health_check_count'] == 3
    finally:
        database.close()


def test_server_drop_inside_recent_window_does_not_prevent_replacement(monkeypatch):
    import os
    monkeypatch.setenv('BROWSE_DB_POOL_MAX', '1')
    monkeypatch.setenv('BROWSE_DB_CHECK_INTERVAL_SECONDS', '1')
    database = BrowseDatabase()
    database.open()
    try:
        with patch('trafficxx.database_pool.monotonic', return_value=0):
            with database.read_connection() as conn:
                first = conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
            with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as admin:
                assert admin.execute('SELECT pg_terminate_backend(%s)', (first,)).fetchone()[0]
            # A connection dropped within the recency window may surface one
            # failure. No application query is retried by this implementation.
            try:
                with database.read_connection() as conn:
                    conn.execute('SELECT 1')
            except psycopg.OperationalError:
                pass
            with database.read_connection() as conn:
                assert conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] != first
    finally:
        database.close()


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
