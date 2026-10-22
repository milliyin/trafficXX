"""Read-only inbox/projection/heartbeat observation; no broker calls or SQL writes."""
import json
import logging
import os
import time

CLIENT = '91f335cf-9de4-4264-9493-eec3cf86f941'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
CURRENT_CHECK = 'preflight'
LAST_SNAPSHOT = None


def snapshot(connection):
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    session = connection.execute("""
        SELECT current_user AS identity, current_database() AS database, s.ssl, s.version,
               pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
        FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()
    """).fetchone()
    if (not session or session['identity'] != 'trafficxx-lab-migrator'
            or session['database'] != 'trafficxx' or not session['ssl'] or session['admin']):
        raise ValueError('Unexpected read-only observer identity or TLS')
    counts = connection.execute("""
        WITH latest AS (
            SELECT DISTINCT ON (reservation_id) reservation_id, id, event_type,
                   payload ->> 'event_id' AS event_id
            FROM public.outbox ORDER BY reservation_id, id DESC
        ), coverage AS (
            SELECT o.id, o.published_at, i.message_id AS inbox_id,
                   i.message_id IS NOT NULL AND (
                     (i.envelope - 'occurred_at') IS DISTINCT FROM jsonb_build_object(
                       'schema_version', 1, 'message_id', o.message_id::text,
                       'outbox_id', o.id, 'reservation_id', o.reservation_id::text,
                       'event_id', o.payload ->> 'event_id', 'status', o.event_type)
                     OR (i.envelope ->> 'occurred_at')::timestamptz IS DISTINCT FROM o.created_at
                   ) AS payload_mismatch
            FROM public.outbox o LEFT JOIN public.consumer_inbox i ON i.message_id = o.message_id
        )
        SELECT (SELECT count(*) FROM coverage) AS total_count,
               (SELECT count(*) FROM coverage WHERE published_at IS NULL) AS pending_count,
               (SELECT count(*) FROM coverage WHERE inbox_id IS NOT NULL) AS inbox_count,
               (SELECT count(*) FROM coverage WHERE inbox_id IS NULL) AS missing_inbox_count,
               (SELECT count(*) FROM coverage WHERE payload_mismatch) AS payload_mismatch_count,
               (SELECT count(*) FROM latest) AS expected_projection_count,
               (SELECT count(*) FROM latest l LEFT JOIN public.reservation_updates p
                  ON p.reservation_id = l.reservation_id
                WHERE p.reservation_id IS NULL OR p.event_id IS DISTINCT FROM l.event_id
                   OR p.status IS DISTINCT FROM l.event_type OR p.last_outbox_id IS DISTINCT FROM l.id
               ) AS projection_mismatch_count,
               (SELECT count(*) FROM public.consumer_inbox i LEFT JOIN public.outbox o
                  ON o.message_id = i.message_id WHERE o.id IS NULL) AS orphan_inbox_count,
               (SELECT count(*) FROM public.reservation_updates p LEFT JOIN latest l
                  ON l.reservation_id = p.reservation_id WHERE l.reservation_id IS NULL
               ) AS orphan_projection_count,
               (SELECT count(*) FROM public.worker_failures) AS failure_record_count,
               (SELECT count(*) FROM public.observed_dead_letters) AS observed_dead_letter_count
    """).fetchone()
    beat = connection.execute("""
        SELECT last_error,
               extract(epoch FROM (clock_timestamp() - last_seen_at))::double precision AS age_seconds
        FROM public.worker_heartbeats WHERE name = 'consumer'
    """).fetchone()
    if not counts:
        raise ValueError('No processing count result')
    return {**counts, 'heartbeat_present': beat is not None,
            'heartbeat_age_seconds': beat['age_seconds'] if beat else None,
            'heartbeat_has_error': beat['last_error'] is not None if beat else None,
            'observer_identity': session['identity'], 'ssl': session['ssl'],
            'tls_version': session['version'], 'read_only': True}


def drained(state):
    age = state['heartbeat_age_seconds']
    return (state['total_count'] > 0 and state['pending_count'] == 0
            and state['inbox_count'] == state['total_count']
            and state['missing_inbox_count'] == 0 and state['payload_mismatch_count'] == 0
            and state['expected_projection_count'] > 0 and state['projection_mismatch_count'] == 0
            and state['orphan_inbox_count'] == 0 and state['orphan_projection_count'] == 0
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
            return {'result': 'CONSUMER_DRAIN_OK', **LAST_SNAPSHOT}
        CURRENT_CHECK = 'await_drain_and_fresh_heartbeat'
        if time.monotonic() >= deadline:
            raise TimeoutError('Inbox/projection incomplete or consumer heartbeat not healthy')
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'CONSUMER_DRAIN_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'snapshot': LAST_SNAPSHOT}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
