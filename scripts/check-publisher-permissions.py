"""User-run publisher-role SQL checks; all diagnostic rows are rolled back."""
import base64
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

CLIENT = 'abd012d6-9e05-415d-b3f5-8c24bee54576'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
ROLE = 'trafficxx-lab-publisher'
CURRENT_CHECK = 'startup'
DENIED = {
    'reservation_read': 'SELECT * FROM public.reservations LIMIT 0',
    'reservation_insert': "INSERT INTO public.reservations(id) VALUES ('00000000-0000-0000-0000-000000000001')",
    'reservation_update': "UPDATE public.reservations SET status = 'EXPIRED' WHERE false",
    'event_read': 'SELECT * FROM public.events LIMIT 0',
    'ticket_read': 'SELECT * FROM public.ticket_units LIMIT 0',
    'ticket_update': 'UPDATE public.ticket_units SET reservation_id = NULL WHERE false',
    'outbox_insert': "INSERT INTO public.outbox(reservation_id) VALUES ('00000000-0000-0000-0000-000000000001')",
    'outbox_payload_update': 'UPDATE public.outbox SET payload = payload WHERE false',
    'outbox_message_identity_update': 'UPDATE public.outbox SET message_id = message_id WHERE false',
    'outbox_reservation_update': 'UPDATE public.outbox SET reservation_id = reservation_id WHERE false',
    'outbox_event_type_update': 'UPDATE public.outbox SET event_type = event_type WHERE false',
    'outbox_occurrence_update': 'UPDATE public.outbox SET created_at = created_at WHERE false',
    'outbox_delete': 'DELETE FROM public.outbox WHERE false',
    'heartbeat_rename': 'UPDATE public.worker_heartbeats SET name = name WHERE false',
    'heartbeat_time_read': 'SELECT last_seen_at FROM public.worker_heartbeats LIMIT 0',
    'heartbeat_time_insert': "INSERT INTO public.worker_heartbeats(name,last_seen_at) VALUES ('__forbidden_probe__',clock_timestamp())",
    'consumer_inbox_read': 'SELECT * FROM public.consumer_inbox LIMIT 0',
    'projection_read': 'SELECT * FROM public.reservation_updates LIMIT 0',
    'failure_table_read': 'SELECT * FROM public.worker_failures LIMIT 0',
    'dead_letter_read': 'SELECT * FROM public.observed_dead_letters LIMIT 0',
    'migration_ledger_read': 'SELECT * FROM public.schema_migrations LIMIT 0',
    'create_table': 'CREATE TABLE public.trafficxx_publisher_permission_probe (id integer)',
    'create_schema': 'CREATE SCHEMA trafficxx_publisher_permission_probe',
}


def positive_checks(connection):
    global CURRENT_CHECK
    allowed = []

    def execute(name, sql, params=None):
        global CURRENT_CHECK
        CURRENT_CHECK = 'allowed.' + name
        cursor = connection.execute(sql, params)
        allowed.append(name)
        return cursor

    def one(cursor):
        if cursor.rowcount != 1:
            raise ValueError('Expected exactly one changed diagnostic row')

    row = execute('outbox_lock', 'SELECT * FROM public.outbox WHERE published_at IS NULL '
                  'AND next_attempt_at <= clock_timestamp() ORDER BY id '
                  'LIMIT 1 FOR UPDATE SKIP LOCKED').fetchone()
    if row is None:
        raise ValueError('No pending due outbox fixture; do not reset or invent events')
    outbox_id = row['id']
    one(execute('attempt_update', 'UPDATE public.outbox SET publish_attempts = publish_attempts + 1, '
                'last_attempt_at = clock_timestamp() WHERE id = %s', (outbox_id,)))
    one(execute('retry_update', 'UPDATE public.outbox SET publish_failures = publish_failures + 1, '
                "last_error = 'BROKER_SEND_FAILED', next_attempt_at = clock_timestamp() "
                "+ %s * interval '1 second' WHERE id = %s", (60, outbox_id)))
    retry = execute('retry_readback', 'SELECT publish_attempts, publish_failures, last_error, '
                    'next_attempt_at > clock_timestamp() AS delayed, '
                    'last_attempt_at IS NOT NULL AS attempted FROM public.outbox WHERE id = %s',
                    (outbox_id,)).fetchone()
    if (not retry or retry['publish_attempts'] != row['publish_attempts'] + 1
            or retry['publish_failures'] != row['publish_failures'] + 1
            or retry['last_error'] != 'BROKER_SEND_FAILED' or not retry['delayed']
            or not retry['attempted']):
        raise ValueError('Retry state mismatch')
    one(execute('publication_update', 'UPDATE public.outbox SET published_at = clock_timestamp(), '
                'last_error = NULL WHERE id = %s', (outbox_id,)))
    marked = execute('publication_readback', 'SELECT published_at IS NOT NULL AS marked, '
                     'last_error, message_id, payload FROM public.outbox WHERE id = %s',
                     (outbox_id,)).fetchone()
    if (not marked or not marked['marked'] or marked['last_error'] is not None
            or marked['message_id'] != row['message_id'] or marked['payload'] != row['payload']):
        raise ValueError('Publication state mismatch')
    name = '__publisher_permission_probe__' + uuid4().hex
    sql = ('INSERT INTO public.worker_heartbeats(name, last_error) VALUES (%s, %s) '
           'ON CONFLICT (name) DO UPDATE SET last_seen_at = clock_timestamp(), '
           'last_error = EXCLUDED.last_error')
    one(execute('heartbeat_insert', sql, (name, None)))
    one(execute('heartbeat_conflict_update', sql, (name, 'PROBE_ERROR')))
    beat = execute('heartbeat_readback', 'SELECT name, last_error FROM public.worker_heartbeats '
                   'WHERE name = %s', (name,)).fetchone()
    if not beat or beat['name'] != name or beat['last_error'] != 'PROBE_ERROR':
        raise ValueError('Heartbeat upsert mismatch')
    # No broker transport is imported or called; caller rolls back this whole
    # transaction before reporting success. Existing outbox state is restored.
    return allowed


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
    allowed = positive_checks(connection)
    for name, sql in DENIED.items():
        expect_denied(connection, name, sql)
    return {'result': 'PUBLISHER_PERMISSIONS_OK', 'identity': ROLE, 'allowed': allowed,
            'denied': list(DENIED), 'denial_sqlstate': '42501', 'diagnostic_rows_committed': False,
            'broker_messages_sent': 0}


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
    if claims.get('sub') != 'system:serviceaccount:trafficxx:publisher':
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
        print(json.dumps({'result': 'PUBLISHER_PERMISSIONS_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'sqlstate': state}))
        sys.exit(1)
