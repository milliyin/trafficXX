"""Read-only outbox/heartbeat observation; no broker send or receive calls."""
import json
import logging
import os
import time

CLIENT = '91f335cf-9de4-4264-9493-eec3cf86f941'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
CURRENT_CHECK = 'preflight'
LAST_SNAPSHOT = None


def snapshot(connection):
    connection.execute('SET TRANSACTION READ ONLY')
    session = connection.execute("""
        SELECT current_user AS identity, current_database() AS database, s.ssl, s.version,
               pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
        FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()
    """).fetchone()
    if (not session or session['identity'] != 'trafficxx-lab-migrator'
            or session['database'] != 'trafficxx' or not session['ssl'] or session['admin']):
        raise ValueError('Unexpected read-only observer identity or TLS')
    counts = connection.execute("""
        SELECT count(*) AS total_count,
               count(*) FILTER (WHERE published_at IS NULL) AS pending_count,
               count(*) FILTER (WHERE published_at IS NOT NULL) AS published_count,
               COALESCE(sum(publish_failures), 0)::bigint AS failed_attempts
        FROM public.outbox
    """).fetchone()
    beat = connection.execute("""
        SELECT last_error,
               extract(epoch FROM (clock_timestamp() - last_seen_at))::double precision AS age_seconds
        FROM public.worker_heartbeats WHERE name = 'publisher'
    """).fetchone()
    if not counts:
        raise ValueError('No outbox count result')
    return {**counts, 'heartbeat_present': beat is not None,
            'heartbeat_age_seconds': beat['age_seconds'] if beat else None,
            'heartbeat_has_error': beat['last_error'] is not None if beat else None,
            'observer_identity': session['identity'], 'ssl': session['ssl'],
            'tls_version': session['version'], 'read_only': True}


def drained(state):
    age = state['heartbeat_age_seconds']
    return (state['total_count'] > 0 and state['pending_count'] == 0
            and state['published_count'] == state['total_count']
            and state['heartbeat_present'] and state['heartbeat_has_error'] is False
            and age is not None and 0 <= age <= 30)


def run():
    global CURRENT_CHECK, LAST_SNAPSHOT
    CURRENT_CHECK, LAST_SNAPSHOT = 'preflight', None
    expected = {'AZURE_CLIENT_ID': CLIENT, 'AZURE_TENANT_ID': TENANT,
                'DATABASE_AUTH_MODE': 'workload_identity', 'PGUSER': 'trafficxx-lab-migrator',
                'PGHOST': 'trafficxx-pg-e8708a85.postgres.database.azure.com', 'PGDATABASE': 'trafficxx'}
    if (any(os.environ.get(key) != value for key, value in expected.items())
            or os.environ.get('DATABASE_URL')):
        raise ValueError('Unexpected observer database/identity configuration')
    from trafficxx.db import connect

    deadline = time.monotonic() + 120
    while True:
        CURRENT_CHECK = 'read_only_snapshot'
        with connect() as connection:
            LAST_SNAPSHOT = snapshot(connection)
        if drained(LAST_SNAPSHOT):
            return {'result': 'PUBLISHER_DRAIN_OK', **LAST_SNAPSHOT}
        CURRENT_CHECK = 'await_drain_and_fresh_heartbeat'
        if time.monotonic() >= deadline:
            raise TimeoutError('Outbox not drained or publisher heartbeat not healthy')
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'PUBLISHER_DRAIN_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'snapshot': LAST_SNAPSHOT}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
