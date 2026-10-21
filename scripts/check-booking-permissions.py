"""User-run booking-role SQL checks; all diagnostic rows are rolled back."""
import base64
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

CLIENT = 'b7cf76ca-6692-432f-8e0e-088b42d9bff0'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
ROLE = 'trafficxx-lab-booking'
CURRENT_CHECK = 'startup'
DENIED = {
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
    'create_table': 'CREATE TABLE public.trafficxx_booking_permission_probe (id integer)',
    'create_schema': 'CREATE SCHEMA trafficxx_booking_permission_probe',
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

    def changed_one(cursor):
        if cursor.rowcount != 1:
            raise ValueError('Expected exactly one changed diagnostic row')

    event_id, customer, key, reservation_id = 'demo-general', '__permission_probe__', uuid4().hex, uuid4()
    request = {'event_id': event_id, 'quantity': 1}
    execute('idempotency_lock', 'SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
            (f'{len(customer)}:{customer}{key}',))
    event = execute('event_lock', 'SELECT * FROM public.events WHERE id = %s FOR UPDATE',
                    (event_id,)).fetchone()
    if not event or event['inventory_type'] != 'GENERAL':
        raise ValueError('Expected the existing general-admission demo event')
    changed_one(execute('event_name_update', 'UPDATE public.events SET name = name WHERE id = %s', (event_id,)))
    unit = execute('free_unit', 'SELECT unit_number FROM public.ticket_units WHERE event_id = %s '
                   'AND reservation_id IS NULL ORDER BY unit_number LIMIT 1', (event_id,)).fetchone()
    if not unit:
        raise ValueError('No free demo unit; do not reset inventory to run this check')
    row = execute('reservation_insert', """
        INSERT INTO public.reservations(id, event_id, customer_id, idempotency_key, request,
                                        quantity, status, expires_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, 1, 'HELD', clock_timestamp() + interval '5 minutes')
        RETURNING *
    """, (reservation_id, event_id, customer, key, json.dumps(request))).fetchone()
    if not row or row['id'] != reservation_id or row['status'] != 'HELD' or row['quantity'] != 1:
        raise ValueError('Unexpected diagnostic reservation')
    previous = execute('idempotency_lookup', 'SELECT * FROM public.reservations '
                       'WHERE customer_id = %s AND idempotency_key = %s', (customer, key)).fetchone()
    if not previous or previous['id'] != reservation_id or previous['request'] != request:
        raise ValueError('Diagnostic idempotency lookup failed')
    changed_one(execute('ticket_claim', 'UPDATE public.ticket_units SET reservation_id = %s '
                        'WHERE event_id = %s AND unit_number = %s AND reservation_id IS NULL',
                        (reservation_id, event_id, unit['unit_number'])))

    def emit(status):
        payload = json.dumps({'reservation_id': str(reservation_id), 'status': status, 'event_id': event_id})
        changed_one(execute('outbox_' + status.lower(), 'INSERT INTO public.outbox '
                            '(reservation_id, event_type, payload) VALUES (%s, %s, %s::jsonb)',
                            (reservation_id, status, payload)))

    # Exercise actual defaults and implicit identity generation with no outbox
    # SELECT/RETURNING and no extra sequence privileges assumed by the test.
    emit('HELD')
    cancelled = execute('reservation_cancel', "UPDATE public.reservations SET status = 'CANCELLED' "
                        "WHERE id = %s AND status = 'HELD' RETURNING *", (reservation_id,)).fetchone()
    if not cancelled or cancelled['status'] != 'CANCELLED':
        raise ValueError('Diagnostic status update failed')
    changed_one(execute('ticket_release', 'UPDATE public.ticket_units SET reservation_id = NULL '
                        'WHERE event_id = %s AND unit_number = %s AND reservation_id = %s',
                        (event_id, unit['unit_number'], reservation_id)))
    emit('CANCELLED')
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
    return {'result': 'BOOKING_PERMISSIONS_OK', 'identity': ROLE, 'allowed': allowed,
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
    if claims.get('sub') != 'system:serviceaccount:trafficxx:booking':
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
        print(json.dumps({'result': 'BOOKING_PERMISSIONS_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, 'sqlstate': state}))
        sys.exit(1)
