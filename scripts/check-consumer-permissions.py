"""User-run consumer-role SQL checks; all diagnostic rows are rolled back."""
import base64
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

CLIENT = '35057245-c3ec-4eba-b182-60549501df42'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
ROLE = 'trafficxx-lab-consumer'
CURRENT_CHECK = 'startup'
DENIED = {
    'reservation_read': 'SELECT * FROM public.reservations LIMIT 0',
    'reservation_insert': "INSERT INTO public.reservations(id) VALUES ('00000000-0000-0000-0000-000000000001')",
    'reservation_update': "UPDATE public.reservations SET status = 'EXPIRED' WHERE false",
    'event_read': 'SELECT * FROM public.events LIMIT 0',
    'ticket_read': 'SELECT * FROM public.ticket_units LIMIT 0',
    'ticket_update': 'UPDATE public.ticket_units SET reservation_id = NULL WHERE false',
    'outbox_read': 'SELECT * FROM public.outbox LIMIT 0',
    'outbox_insert': "INSERT INTO public.outbox(reservation_id) VALUES ('00000000-0000-0000-0000-000000000001')",
    'outbox_update': 'UPDATE public.outbox SET published_at = NULL WHERE false',
    'inbox_update': 'UPDATE public.consumer_inbox SET envelope = envelope WHERE false',
    'inbox_delete': 'DELETE FROM public.consumer_inbox WHERE false',
    'inbox_time_read': 'SELECT processed_at FROM public.consumer_inbox LIMIT 0',
    'inbox_time_insert': "INSERT INTO public.consumer_inbox(message_id,envelope,processed_at) VALUES (gen_random_uuid(),'{}',clock_timestamp())",
    'projection_key_update': 'UPDATE public.reservation_updates SET reservation_id = reservation_id WHERE false',
    'projection_time_read': 'SELECT updated_at FROM public.reservation_updates LIMIT 0',
    'projection_time_insert': "INSERT INTO public.reservation_updates(reservation_id,updated_at) VALUES (gen_random_uuid(),clock_timestamp())",
    'projection_delete': 'DELETE FROM public.reservation_updates WHERE false',
    'heartbeat_rename': 'UPDATE public.worker_heartbeats SET name = name WHERE false',
    'heartbeat_time_read': 'SELECT last_seen_at FROM public.worker_heartbeats LIMIT 0',
    'heartbeat_time_insert': "INSERT INTO public.worker_heartbeats(name,last_seen_at) VALUES ('__forbidden_probe__',clock_timestamp())",
    'failure_read': 'SELECT * FROM public.worker_failures LIMIT 0',
    'failure_update': "UPDATE public.worker_failures SET error_code = 'PROBE' WHERE false",
    'failure_id_insert': "INSERT INTO public.worker_failures(id,message_id,error_code) OVERRIDING SYSTEM VALUE VALUES (1,'probe','PROBE')",
    'failure_time_insert': "INSERT INTO public.worker_failures(message_id,error_code,occurred_at) VALUES ('probe','PROBE',clock_timestamp())",
    'dead_letter_read': 'SELECT * FROM public.observed_dead_letters LIMIT 0',
    'dead_letter_update': "UPDATE public.observed_dead_letters SET reason = 'PROBE' WHERE false",
    'dead_letter_time_insert': "INSERT INTO public.observed_dead_letters(queue_name,sequence_number,message_id,reason,observed_at) VALUES ('probe',1,'probe','PROBE',clock_timestamp())",
    'migration_ledger_read': 'SELECT * FROM public.schema_migrations LIMIT 0',
    'create_table': 'CREATE TABLE public.trafficxx_consumer_permission_probe (id integer)',
    'create_schema': 'CREATE SCHEMA trafficxx_consumer_permission_probe',
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

    def count(cursor, expected=1):
        if cursor.rowcount != expected:
            raise ValueError('Unexpected diagnostic row count')

    # This known, previously verified EXPIRED reservation supplies the foreign key.
    # No reservation read/write is needed; a missing fixture must fail, not invent one.
    from uuid import UUID
    reservation = UUID(os.environ['PROBE_RESERVATION_ID'])
    message = uuid4()
    payload = {'probe': message.hex}
    inbox = ('INSERT INTO public.consumer_inbox(message_id, envelope) VALUES (%s, %s::jsonb) '
             'ON CONFLICT DO NOTHING RETURNING message_id')
    row = execute('inbox_insert', inbox, (message, json.dumps(payload))).fetchone()
    if not row or row['message_id'] != message:
        raise ValueError('Inbox insert mismatch')
    if execute('inbox_duplicate', inbox, (message, json.dumps(payload))).fetchone() is not None:
        raise ValueError('Duplicate inbox insert was not suppressed')
    row = execute('inbox_readback', 'SELECT envelope FROM public.consumer_inbox WHERE message_id = %s',
                  (message,)).fetchone()
    if not row or row['envelope'] != payload:
        raise ValueError('Inbox payload mismatch')
    previous = execute('projection_lookup', 'SELECT last_outbox_id FROM public.reservation_updates '
                       'WHERE reservation_id = %s', (reservation,)).fetchone()
    version = previous['last_outbox_id'] + 1 if previous else 1
    projection = (
        'INSERT INTO public.reservation_updates(reservation_id, event_id, status, last_outbox_id) '
        'VALUES (%s, %s, %s, %s) ON CONFLICT (reservation_id) DO UPDATE '
        'SET event_id = EXCLUDED.event_id, status = EXCLUDED.status, '
        'last_outbox_id = EXCLUDED.last_outbox_id, updated_at = clock_timestamp() '
        'WHERE reservation_updates.last_outbox_id < EXCLUDED.last_outbox_id')
    count(execute('projection_upsert', projection, (reservation, 'demo-seated', 'HELD', version)))
    count(execute('projection_newer_update', projection, (reservation, 'demo-seated', 'EXPIRED', version + 1)))
    count(execute('projection_older_ignored', projection, (reservation, 'demo-seated', 'HELD', version)), 0)
    row = execute('projection_readback', 'SELECT reservation_id, event_id, status, last_outbox_id '
                  'FROM public.reservation_updates WHERE reservation_id = %s', (reservation,)).fetchone()
    if (not row or row['reservation_id'] != reservation or row['event_id'] != 'demo-seated'
            or row['status'] != 'EXPIRED' or row['last_outbox_id'] != version + 1):
        raise ValueError('Projection ordering mismatch')
    name = '__consumer_permission_probe__' + message.hex
    heartbeat = ('INSERT INTO public.worker_heartbeats(name, last_error) VALUES (%s, %s) '
                 'ON CONFLICT (name) DO UPDATE SET last_seen_at = clock_timestamp(), '
                 'last_error = EXCLUDED.last_error')
    count(execute('heartbeat_insert', heartbeat, (name, None)))
    count(execute('heartbeat_conflict_update', heartbeat, (name, 'PROBE_ERROR')))
    row = execute('heartbeat_readback', 'SELECT name, last_error FROM public.worker_heartbeats '
                  'WHERE name = %s', (name,)).fetchone()
    if not row or row['name'] != name or row['last_error'] != 'PROBE_ERROR':
        raise ValueError('Heartbeat upsert mismatch')
    count(execute('failure_insert', 'INSERT INTO public.worker_failures(message_id, error_code) '
                  'VALUES (%s, %s)', (str(message), 'PROBE_ERROR')))
    dead_letter = ('INSERT INTO public.observed_dead_letters(queue_name, sequence_number, message_id, reason) '
                   'VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING')
    args = (name, 1, str(message), 'PROBE_REASON')
    count(execute('dead_letter_insert', dead_letter, args))
    count(execute('dead_letter_duplicate', dead_letter, args), 0)
    # No broker imports, receives or settlements. Caller rolls everything back.
    # Generated identity sequence increments can survive rollback (normal PostgreSQL behavior).
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
    return {'result': 'CONSUMER_PERMISSIONS_OK', 'identity': ROLE, 'allowed': allowed,
            'denied': list(DENIED), 'denial_sqlstate': '42501', 'diagnostic_rows_committed': False,
            'broker_messages_sent': 0, 'broker_messages_received': 0}


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
    if claims.get('sub') != 'system:serviceaccount:trafficxx:consumer':
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
        print(json.dumps({'result': 'CONSUMER_PERMISSIONS_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'sqlstate': state}))
        sys.exit(1)
