"""Before/after fingerprints for a controlled duplicate-delivery experiment."""
import json
import logging
import os

CLIENT = '91f335cf-9de4-4264-9493-eec3cf86f941'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
CURRENT_CHECK = 'preflight'
LAST_SNAPSHOT = None


def fingerprint(value):
    import hashlib
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def snapshot(connection):
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    session = connection.execute("""
        SELECT current_user AS identity, current_database() AS database, s.ssl, s.version,
               pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
        FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()
    """).fetchone()
    if (not session or session['identity'] != 'trafficxx-lab-migrator'
            or session['database'] != 'trafficxx' or not session['ssl'] or session['admin']):
        raise ValueError('Unexpected observer identity or TLS')
    target = connection.execute("""
        SELECT o.id, o.message_id::text AS message_id, o.published_at IS NOT NULL AS published,
               i.envelope IS NOT NULL AS processed
        FROM public.outbox o LEFT JOIN public.consumer_inbox i ON i.message_id = o.message_id
        WHERE o.id = 3
    """).fetchone()
    if not target or not target['published'] or not target['processed']:
        raise ValueError('Previously processed outbox row 3 is required')
    tables = {}
    for table, order in (('consumer_inbox', 'message_id'),
                         ('reservation_updates', 'reservation_id'), ('outbox', 'id')):
        # Fixed identifiers only. Include every column, including row timestamps.
        row = connection.execute(
            'SELECT count(*) AS count, COALESCE(jsonb_agg(to_jsonb(t) ORDER BY '
            + order + ")::text, '[]') AS contents FROM public." + table + ' t').fetchone()
        if not row or row['count'] <= 0:
            raise ValueError('Expected nonempty diagnostic source table')
        tables[table] = {'count': row['count'], 'sha256': fingerprint(row['contents'])}
    return {'result': 'DUPLICATE_BASELINE_OK', 'outbox_id': target['id'],
            'message_id': target['message_id'], 'tables': tables,
            'observer_identity': session['identity'], 'ssl': session['ssl'],
            'tls_version': session['version'], 'read_only': True}


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

    CURRENT_CHECK = 'read_only_baseline'
    with connect() as connection:
        LAST_SNAPSHOT = snapshot(connection)
    return LAST_SNAPSHOT


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'DUPLICATE_BASELINE_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'snapshot': LAST_SNAPSHOT}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
