"""User-run operations-role monitoring reads/denials; no diagnostic rows committed."""
import base64
import json
import logging
import os
from pathlib import Path
import sys

CLIENT = '8e14cc93-46e3-466d-bec1-5dda5b3cc4ef'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
ROLE = 'trafficxx-lab-operations'
CURRENT_CHECK = 'startup'
# The same SQL expressions currently used by operations.snapshot().
BACKLOG_SQL = 'SELECT count(*) FILTER (WHERE o.published_at IS NULL) AS outbox_pending, coalesce(greatest(0, extract(epoch FROM clock_timestamp() - min(o.created_at) FILTER (WHERE o.published_at IS NULL))), 0) AS outbox_oldest_seconds, count(*) FILTER (WHERE o.published_at IS NOT NULL AND i.message_id IS NULL) AS processing_outstanding, coalesce(greatest(0, extract(epoch FROM clock_timestamp() - min(o.created_at) FILTER (WHERE o.published_at IS NOT NULL AND i.message_id IS NULL))), 0) AS processing_oldest_seconds, coalesce(sum(o.publish_failures), 0) AS publish_failures_total FROM outbox o LEFT JOIN consumer_inbox i ON i.message_id = o.message_id'
COUNTS_SQL = 'SELECT (SELECT count(*) FROM consumer_inbox) AS processed_total, (SELECT count(*) FROM worker_failures) AS processing_failures_total, (SELECT count(*) FROM observed_dead_letters) AS observed_dead_letters_total'
HEARTBEATS_SQL = 'SELECT name, last_seen_at, last_error, greatest(0, extract(epoch FROM clock_timestamp() - last_seen_at)) AS age_seconds FROM worker_heartbeats ORDER BY name'

DENIED = {
    'reservation_read': 'SELECT * FROM public.reservations LIMIT 0',
    'reservation_write': "UPDATE public.reservations SET status = 'EXPIRED' WHERE false",
    'event_read': 'SELECT * FROM public.events LIMIT 0',
    'inventory_read': 'SELECT * FROM public.ticket_units LIMIT 0',
    'inventory_write': 'UPDATE public.ticket_units SET reservation_id = NULL WHERE false',
    'outbox_payload': 'SELECT payload FROM public.outbox LIMIT 0',
    'outbox_reservation': 'SELECT reservation_id FROM public.outbox LIMIT 0',
    'outbox_insert': "INSERT INTO public.outbox(reservation_id) VALUES (gen_random_uuid())",
    'outbox_update': 'UPDATE public.outbox SET published_at = NULL WHERE false',
    'outbox_delete': 'DELETE FROM public.outbox WHERE false',
    'inbox_envelope': 'SELECT envelope FROM public.consumer_inbox LIMIT 0',
    'inbox_timestamp': 'SELECT processed_at FROM public.consumer_inbox LIMIT 0',
    'inbox_insert': 'INSERT INTO public.consumer_inbox(message_id) VALUES (gen_random_uuid())',
    'inbox_update': 'UPDATE public.consumer_inbox SET message_id = message_id WHERE false',
    'inbox_delete': 'DELETE FROM public.consumer_inbox WHERE false',
    'projection_read': 'SELECT * FROM public.reservation_updates LIMIT 0',
    'heartbeat_insert': "INSERT INTO public.worker_heartbeats(name) VALUES ('__operations_probe__')",
    'heartbeat_update': 'UPDATE public.worker_heartbeats SET last_error = NULL WHERE false',
    'heartbeat_delete': 'DELETE FROM public.worker_heartbeats WHERE false',
    'failure_details': 'SELECT message_id, error_code FROM public.worker_failures LIMIT 0',
    'failure_insert': "INSERT INTO public.worker_failures(message_id,error_code) VALUES ('probe','PROBE')",
    'failure_update': "UPDATE public.worker_failures SET error_code = 'PROBE' WHERE false",
    'failure_delete': 'DELETE FROM public.worker_failures WHERE false',
    'dead_letter_details': 'SELECT message_id, reason FROM public.observed_dead_letters LIMIT 0',
    'dead_letter_insert': "INSERT INTO public.observed_dead_letters(queue_name,sequence_number,message_id,reason) VALUES ('probe',1,'probe','PROBE')",
    'dead_letter_update': "UPDATE public.observed_dead_letters SET queue_name = queue_name WHERE false",
    'dead_letter_delete': 'DELETE FROM public.observed_dead_letters WHERE false',
    'migration_ledger_read': 'SELECT * FROM public.schema_migrations LIMIT 0',
    'create_table': 'CREATE TABLE public.trafficxx_operations_permission_probe (id integer)',
    'create_schema': 'CREATE SCHEMA trafficxx_operations_permission_probe',
}


def positive_checks(connection):
    global CURRENT_CHECK
    import math
    allowed = []

    def execute(name, sql):
        global CURRENT_CHECK
        CURRENT_CHECK = 'allowed.' + name
        cursor = connection.execute(sql)
        allowed.append(name)
        return cursor

    row = execute('readiness', 'SELECT 1 AS alive').fetchone()
    if not row or row['alive'] != 1:
        raise ValueError('Readiness query failed')
    backlog = execute('backlog_join_and_ages', BACKLOG_SQL).fetchone()
    totals = execute('count_only_sources', COUNTS_SQL).fetchone()
    if not backlog or not totals:
        raise ValueError('Missing monitoring aggregates')
    metrics = {**backlog, **totals}
    expected = {'outbox_pending', 'outbox_oldest_seconds', 'processing_outstanding',
                'processing_oldest_seconds', 'publish_failures_total', 'processed_total',
                'processing_failures_total', 'observed_dead_letters_total'}
    if set(metrics) != expected:
        raise ValueError('Unexpected monitoring aggregate fields')
    for key, value in metrics.items():
        if not math.isfinite(float(value)) or value < 0:
            raise ValueError('Invalid monitoring aggregate value')
        metrics[key] = float(value) if key.endswith('_seconds') else int(value)
    rows = execute('worker_heartbeat_reads', HEARTBEATS_SQL).fetchall()
    workers = []
    for row in rows:
        age = float(row['age_seconds'])
        if not math.isfinite(age) or age < 0:
            raise ValueError('Invalid heartbeat age')
        workers.append({'name': row['name'], 'age_seconds': age,
                        'has_error': row['last_error'] is not None})
    return allowed, {'metrics': metrics, 'workers': workers, 'native_queue_counts_available': False}


def expect_denied(connection, name, sql):
    global CURRENT_CHECK
    CURRENT_CHECK = 'denied.' + name
    connection.execute('SAVEPOINT permission_probe')
    try:
        connection.execute(sql)
    except Exception as error:
        connection.execute('ROLLBACK TO SAVEPOINT permission_probe')
        connection.execute('RELEASE SAVEPOINT permission_probe')
        if getattr(error, 'sqlstate', None) != '42501':
            raise
    else:
        connection.execute('ROLLBACK TO SAVEPOINT permission_probe')
        connection.execute('RELEASE SAVEPOINT permission_probe')
        raise ValueError('A forbidden operation was allowed and rolled back')


def verify_permissions(connection):
    allowed, snapshot = positive_checks(connection)
    for name, sql in DENIED.items():
        expect_denied(connection, name, sql)
    return {'result': 'OPERATIONS_PERMISSIONS_OK', 'identity': ROLE, 'allowed': allowed,
            'denied': list(DENIED), 'denial_sqlstate': '42501', 'diagnostic_rows_committed': False,
            'broker_messages_sent': 0, 'broker_messages_received': 0, 'snapshot': snapshot}


def run():
    global CURRENT_CHECK
    CURRENT_CHECK = 'identity_preflight'
    from azure.identity import WorkloadIdentityCredential
    import psycopg
    from psycopg.rows import dict_row

    if os.environ['AZURE_CLIENT_ID'] != CLIENT or os.environ['AZURE_TENANT_ID'] != TENANT:
        raise ValueError('Unexpected injected identity')
    token_file = os.environ['AZURE_FEDERATED_TOKEN_FILE']
    assertion = Path(token_file).read_text().strip()
    payload = assertion.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('sub') != 'system:serviceaccount:trafficxx:operations':
        raise ValueError('Unexpected service account')
    ca = '/etc/ssl/certs/ca-certificates.crt'
    if not Path(ca).is_file():
        raise ValueError('Missing public CA bundle')
    CURRENT_CHECK = 'token_exchange'
    with WorkloadIdentityCredential(tenant_id=TENANT, client_id=CLIENT,
                                    token_file_path=token_file) as credential:
        token = credential.get_token('https://ossrdbms-aad.database.windows.net/.default')
        CURRENT_CHECK = 'sql_login'
        with psycopg.connect(
            host='trafficxx-pg-e8708a85.postgres.database.azure.com', port=5432,
            dbname='trafficxx', user=ROLE, password=token.token,
            sslmode='verify-full', sslrootcert=ca, connect_timeout=10, row_factory=dict_row,
            options='-c search_path=public -c statement_timeout=15000 -c lock_timeout=5000',
        ) as connection:
            connection.execute('SET TRANSACTION READ WRITE')
            session = connection.execute("""
                SELECT current_database() AS database, current_user AS identity, s.ssl, s.version,
                       r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                       pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
                FROM pg_stat_ssl s JOIN pg_roles r ON r.rolname = current_user
                WHERE s.pid = pg_backend_pid()
            """).fetchone()
            if (not session or session['database'] != 'trafficxx' or session['identity'] != ROLE
                    or not session['ssl'] or any(session[key] for key in (
                        'rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolreplication', 'rolbypassrls', 'admin'))):
                raise ValueError('Unexpected SQL identity, TLS or administrator privileges')
            result = verify_permissions(connection)
            CURRENT_CHECK = 'final_rollback'
            connection.rollback()
            result.update({'database': session['database'], 'ssl': session['ssl'],
                           'tls_version': session['version'], 'admin': False})
    print(json.dumps(result))


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        run()
    except Exception as error:
        state = getattr(error, 'sqlstate', None)
        if not isinstance(state, str) or len(state) != 5 or not state.isalnum():
            state = None
        print(json.dumps({'result': 'OPERATIONS_PERMISSIONS_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'sqlstate': state}))
        sys.exit(1)
