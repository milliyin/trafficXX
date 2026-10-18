import os

import pytest

from trafficxx.db import connect
from trafficxx.migrate import run


@pytest.fixture(scope='session', autouse=True)
def migrated_database():
    # These tests intentionally reset inventory. Never run against the lab database.
    if os.environ.get('DATABASE_URL', '').rsplit('/', 1)[-1] != 'trafficxx_test':
        pytest.fail('Tests require the dedicated trafficxx_test database')
    run()


@pytest.fixture(autouse=True)
def fresh_database(migrated_database):
    with connect() as conn:
        conn.execute('TRUNCATE consumer_inbox, reservation_updates, worker_failures, '
                     'observed_dead_letters, worker_heartbeats, outbox, ticket_units, '
                     'reservations, events RESTART IDENTITY CASCADE')
        conn.execute("INSERT INTO events VALUES ('general', 'General test', 'GENERAL'), "
                     "('seated', 'Seated test', 'SEATED')")
        conn.execute("INSERT INTO ticket_units(event_id, unit_number) "
                     "SELECT 'general', n FROM generate_series(1, 100) AS n")
        conn.execute("INSERT INTO ticket_units(event_id, unit_number, seat_label) "
                     "SELECT 'seated', n, 'A' || n FROM generate_series(1, 100) AS n")
