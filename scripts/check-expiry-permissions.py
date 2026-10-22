"""User-run expiry-role SQL checks; all diagnostic rows are rolled back."""
import base64
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

CLIENT = '87cf111e-64b3-4455-8704-7cde6aca1352'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
ROLE = 'trafficxx-lab-expiry'
CURRENT_CHECK = 'startup'
DENIED = {
    'reservation_insert': "INSERT INTO public.reservations(id) VALUES ('00000000-0000-0000-0000-000000000001')",
    'ticket_label_read': 'SELECT seat_label FROM public.ticket_units LIMIT 0',
    'change_event_inventory': 'UPDATE public.events SET inventory_type = inventory_type WHERE false',
    'change_customer': 'UPDATE public.reservations SET customer_id = customer_id WHERE false',
    'extend_hold': 'UPDATE public.reservations SET expires_at = expires_at WHERE false',
    'change_seat_label': 'UPDATE public.ticket_units SET seat_label = seat_label WHERE false',
    'add_inventory': "INSERT INTO public.ticket_units(event_id, unit_number) VALUES ('demo-general', 100001)",
    'delete_reservation': 'DELETE FROM public.reservations WHERE false',
    'outbox_read': 'SELECT * FROM public.outbox LIMIT 0',
    'publish_outbox': 'UPDATE public.outbox SET published_at = clock_timestamp() WHERE false',
    'worker_table_read': 'SELECT * FROM public.consumer_inbox LIMIT 0',
    'migration_ledger_read': 'SELECT * FROM public.schema_migrations LIMIT 0',
    'create_table': 'CREATE TABLE public.trafficxx_expiry_permission_probe (id integer)',
    'create_schema': 'CREATE SCHEMA trafficxx_expiry_permission_probe',
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

    event_id = 'demo-seated'
    event = execute('event_lock', 'SELECT * FROM public.events WHERE id = %s FOR UPDATE',
                    (event_id,)).fetchone()
    if not event or event['inventory_type'] != 'SEATED':
        raise ValueError('Expected the existing seated demo event')
    cursor = execute('event_name_update', 'UPDATE public.events SET name = name WHERE id = %s', (event_id,))
    if cursor.rowcount != 1:
        raise ValueError('Expected one existing demo event')
    fixture = execute('expired_fixture', "SELECT * FROM public.reservations WHERE event_id = %s "
                      "AND status = 'EXPIRED' AND expires_at <= clock_timestamp() "
                      'ORDER BY created_at LIMIT 1 FOR UPDATE', (event_id,)).fetchone()
    if not fixture:
        raise ValueError('Expected expired demo history; do not create/reset inventory as this worker')
    reservation_id = fixture['id']
    assignment = execute('assignment_lookup', 'SELECT reservation_id FROM public.ticket_units '
                         'WHERE reservation_id = %s LIMIT 1', (reservation_id,)).fetchone()
    if assignment:
        raise ValueError('Expired fixture unexpectedly still owns inventory')
    # Re-stage only this expired history row inside the uncommitted diagnostic
    # transaction, using its original expired timestamp. No hold is committed.
    staged = execute('stage_expired_hold', "UPDATE public.reservations SET status = 'HELD' "
                     "WHERE id = %s AND status = 'EXPIRED' RETURNING *", (reservation_id,)).fetchone()
    if not staged or staged['id'] != reservation_id or staged['status'] != 'HELD':
        raise ValueError('Unable to stage the diagnostic row')
    due = execute('due_scan', "SELECT DISTINCT event_id FROM public.reservations "
                  "WHERE status = 'HELD' AND expires_at <= clock_timestamp()").fetchall()
    if not any(row['event_id'] == event_id for row in due):
        raise ValueError('Sweep query did not find the staged due hold')
    expired = execute('expire_update', "UPDATE public.reservations SET status = 'EXPIRED' "
                      "WHERE event_id = %s AND id = %s AND status = 'HELD' "
                      'AND expires_at <= clock_timestamp() RETURNING *',
                      (event_id, reservation_id)).fetchone()
    if not expired or expired['id'] != reservation_id or expired['status'] != 'EXPIRED':
        raise ValueError('Conditional diagnostic expiry failed')
    # This fixture has no assignment: zero matching rows still checks UPDATE
    # and WHERE-column permissions. Actual assigned inventory release is later.
    execute('ticket_release', 'UPDATE public.ticket_units SET reservation_id = NULL '
            'WHERE reservation_id = %s', (reservation_id,))
    payload = json.dumps({'reservation_id': str(reservation_id), 'status': 'EXPIRED', 'event_id': event_id})
    cursor = execute('outbox_insert', 'INSERT INTO public.outbox '
                     '(reservation_id, event_type, payload) VALUES (%s, %s, %s::jsonb)',
                     (reservation_id, '__permission_probe__' + uuid4().hex, payload))
    if cursor.rowcount != 1:
        raise ValueError('Diagnostic outbox insert failed')
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
    return {'result': 'EXPIRY_PERMISSIONS_OK', 'identity': ROLE, 'allowed': allowed,
            'denied': list(DENIED), 'denial_sqlstate': '42501', 'diagnostic_rows_committed': False}


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
    if claims.get('sub') != 'system:serviceaccount:trafficxx:expiry':
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
        print(json.dumps({'result': 'EXPIRY_PERMISSIONS_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'sqlstate': state}))
        sys.exit(1)
