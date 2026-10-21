"""User-run migrator actions for Azure schema, browse grants and demo inventory."""
import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import sys

ROLE = "trafficxx-lab-migrator"
CLIENT = "91f335cf-9de4-4264-9493-eec3cf86f941"
TENANT = "d2828580-3217-43ad-9f3f-8d430998b86e"
TABLES = {
    "events", "reservations", "ticket_units", "outbox", "schema_migrations",
    "consumer_inbox", "reservation_updates", "worker_failures",
    "observed_dead_letters", "worker_heartbeats",
}
FILES = ("001-schema.sql", "002-messaging.sql")


def relations(connection):
    return connection.execute("""
        SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
    """).fetchall()


def bootstrap(connection, payloads):
    """Caller commits or rolls back the complete operation as one transaction."""
    # Same lock as trafficxx.migrate: do not race the additive migration runner.
    connection.execute("SELECT pg_advisory_xact_lock(7432002)")
    before = {row["relname"]: row for row in relations(connection)}
    if "schema_migrations" not in before:
        if before:
            raise ValueError("Refusing to adopt an existing untracked schema")
        connection.execute("""
            CREATE TABLE public.schema_migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """)
    elif before["schema_migrations"]["owner"] != ROLE or before["schema_migrations"]["relkind"] != "r":
        raise ValueError("Unexpected migration ledger owner or relation type")

    ledger = {row["version"]: row["checksum"] for row in connection.execute(
        "SELECT version, checksum FROM public.schema_migrations FOR UPDATE"
    ).fetchall()}
    if FILES[0] not in ledger and (set(before) - {"schema_migrations"} or ledger):
        raise ValueError("Base schema has no bootstrap record; refusing to adopt it")

    # Check every existing checksum before executing any new migration.
    checksums = {name: hashlib.sha256(payloads[name].encode()).hexdigest() for name in FILES}
    for name in FILES:
        if name in ledger and ledger[name] != checksums[name]:
            raise ValueError("An applied schema file was edited")

    applied, skipped = [], []
    for name in FILES:
        if name in ledger:
            skipped.append(name)
            continue
        connection.execute(payloads[name])
        connection.execute(
            "INSERT INTO public.schema_migrations(version, checksum) VALUES (%s, %s)",
            (name, checksums[name]),
        )
        applied.append(name)

    after = {row["relname"]: row for row in relations(connection)}
    if any(name not in after or after[name]["owner"] != ROLE or after[name]["relkind"] != "r"
           for name in TABLES):
        raise ValueError("Missing application table or unexpected table ownership")
    return {"result": "SCHEMA_BOOTSTRAP_OK", "identity": ROLE,
            "applied": applied, "skipped": skipped, "verified_tables": len(TABLES)}


def grant_browse(connection):
    """The table-owning migrator grants only columns used by browse queries."""
    connection.execute("SELECT pg_advisory_xact_lock(7432002)")
    owned = {row["relname"]: row for row in relations(connection)}
    if any(name not in owned or owned[name]["owner"] != ROLE or owned[name]["relkind"] != "r"
           for name in ("events", "ticket_units", "reservations")):
        raise ValueError("Migrator must own all browse source tables")
    connection.execute('GRANT SELECT ON public.events TO "trafficxx-lab-browse"')
    connection.execute('GRANT SELECT (event_id, unit_number, seat_label, reservation_id) '
                       'ON public.ticket_units TO "trafficxx-lab-browse"')
    connection.execute('GRANT SELECT (id, status, expires_at) '
                       'ON public.reservations TO "trafficxx-lab-browse"')
    access = connection.execute("""
        SELECT has_database_privilege('trafficxx-lab-browse', 'trafficxx', 'CONNECT') AS connect,
               has_schema_privilege('trafficxx-lab-browse', 'public', 'USAGE') AS usage,
               has_schema_privilege('trafficxx-lab-browse', 'public', 'CREATE') AS create_objects,
               has_database_privilege('trafficxx-lab-browse', 'trafficxx', 'CREATE') AS create_schema,
               pg_has_role('trafficxx-lab-browse', 'azure_pg_admin', 'MEMBER') AS admin,
               EXISTS (
                   SELECT 1 FROM (VALUES ('public.events'), ('public.ticket_units'),
                                         ('public.reservations')) AS t(name)
                   WHERE has_any_column_privilege('trafficxx-lab-browse', t.name, 'INSERT,UPDATE')
                      OR has_table_privilege('trafficxx-lab-browse', t.name, 'DELETE,TRUNCATE')
               ) AS writes,
               EXISTS (
                   SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'public.reservations'::regclass AND attnum > 0 AND NOT attisdropped
                     AND attname NOT IN ('id', 'status', 'expires_at')
                     AND has_column_privilege('trafficxx-lab-browse', attrelid, attnum, 'SELECT')
               ) AS private_reservations
    """).fetchone()
    if (not access or not access["connect"] or not access["usage"]
            or any(access[key] for key in ("create_objects", "create_schema", "admin", "writes", "private_reservations"))):
        raise ValueError("Unexpected effective browse permissions; administrator review required")
    return {"result": "BROWSE_GRANTS_OK", "identity": ROLE,
            "grantee": "trafficxx-lab-browse", "writes": False,
            "private_reservation_columns": False}


def seed_demo(connection):
    """Insert only absent demo events; validate existing inventory without resets."""
    connection.execute("SELECT pg_advisory_xact_lock(7432002)")
    definitions = (
        ('demo-general', 'Demo concert - general admission', 'GENERAL'),
        ('demo-seated', 'Demo concert - assigned seats', 'SEATED'),
    )
    created, skipped = [], []
    for event_id, name, inventory_type in definitions:
        event = connection.execute(
            'SELECT id, name, inventory_type FROM public.events WHERE id = %s FOR UPDATE',
            (event_id,),
        ).fetchone()
        if event is not None:
            if event['name'] != name or event['inventory_type'] != inventory_type:
                raise ValueError('Existing demo event differs from the expected definition')
            units = connection.execute(
                'SELECT unit_number, seat_label FROM public.ticket_units '
                'WHERE event_id = %s ORDER BY unit_number LIMIT 101', (event_id,),
            ).fetchall()
            expected = [{'unit_number': n, 'seat_label': f'A{n}' if inventory_type == 'SEATED' else None}
                        for n in range(1, 101)]
            if units != expected:
                raise ValueError('Existing demo inventory differs; refusing to repair or reset it')
            skipped.append(event_id)
            continue
        connection.execute(
            'INSERT INTO public.events(id, name, inventory_type) VALUES (%s, %s, %s)',
            (event_id, name, inventory_type),
        )
        connection.execute(
            'INSERT INTO public.ticket_units(event_id, unit_number, seat_label) '
            "SELECT %s, n, CASE WHEN %s = 'SEATED' THEN 'A' || n ELSE NULL END "
            'FROM generate_series(1, 100) AS n', (event_id, inventory_type),
        )
        created.append(event_id)
    return {'result': 'DEMO_SEED_OK', 'identity': ROLE, 'created': created,
            'skipped': skipped, 'events_verified': 2, 'units_per_event': 100}


BOOKING_INSERTS = {
    ('reservations', column) for column in (
        'id', 'event_id', 'customer_id', 'idempotency_key', 'request', 'quantity', 'status', 'expires_at')
} | {('outbox', column) for column in ('reservation_id', 'event_type', 'payload')}
BOOKING_UPDATES = {('events', 'name'), ('reservations', 'status'), ('ticket_units', 'reservation_id')}
BOOKING_READS = {'events', 'reservations', 'ticket_units'}


def grant_booking(connection):
    """Grant the booking service's current SQL contract, rejecting broader access."""
    grantee = 'trafficxx-lab-booking'
    connection.execute('SELECT pg_advisory_xact_lock(7432002)')
    owned = {row['relname']: row for row in relations(connection)}
    if any(name not in owned or owned[name]['owner'] != ROLE or owned[name]['relkind'] != 'r'
           for name in ('events', 'reservations', 'ticket_units', 'outbox')):
        raise ValueError('Migrator must own booking source tables')
    grants = (
        'GRANT SELECT ON public.events, public.reservations, public.ticket_units',
        # PostgreSQL requires UPDATE on at least one column for SELECT FOR UPDATE.
        # This also permits SQL-level display-name edits, not event-ID/type edits.
        'GRANT UPDATE (name) ON public.events',
        'GRANT INSERT (id, event_id, customer_id, idempotency_key, request, quantity, status, expires_at) ON public.reservations',
        'GRANT UPDATE (status) ON public.reservations',
        'GRANT UPDATE (reservation_id) ON public.ticket_units',
        'GRANT INSERT (reservation_id, event_type, payload) ON public.outbox',
    )
    for sql in grants:
        connection.execute(sql + ' TO "trafficxx-lab-booking"')
    access = connection.execute("""
        SELECT has_database_privilege(rolname, 'trafficxx', 'CONNECT') AS connect,
               has_schema_privilege(rolname, 'public', 'USAGE') AS usage,
               has_schema_privilege(rolname, 'public', 'CREATE') AS create_objects,
               has_database_privilege(rolname, 'trafficxx', 'CREATE') AS create_schema,
               pg_has_role(rolname, 'azure_pg_admin', 'MEMBER') AS admin,
               rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
        FROM pg_roles WHERE rolname = %s
    """, (grantee,)).fetchone()
    if (not access or not access['connect'] or not access['usage']
            or any(access[key] for key in ('create_objects', 'create_schema', 'admin', 'rolsuper',
                                           'rolcreatedb', 'rolcreaterole', 'rolreplication', 'rolbypassrls'))):
        raise ValueError('Unexpected effective booking role privileges')
    columns = connection.execute("""
        SELECT c.relname, a.attname,
               has_column_privilege(%s, c.oid, a.attnum, 'SELECT') AS can_read,
               has_column_privilege(%s, c.oid, a.attnum, 'INSERT') AS can_insert,
               has_column_privilege(%s, c.oid, a.attnum, 'UPDATE') AS can_update,
               has_column_privilege(%s, c.oid, a.attnum, 'REFERENCES') AS can_reference,
               has_table_privilege(%s, c.oid, 'DELETE,TRUNCATE,TRIGGER') AS destructive
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
          AND a.attnum > 0 AND NOT a.attisdropped
    """, (grantee,) * 5).fetchall()
    seen = set()
    for column in columns:
        pair = (column['relname'], column['attname'])
        seen.add(pair)
        if (column['can_read'] != (pair[0] in BOOKING_READS)
                or column['can_insert'] != (pair in BOOKING_INSERTS)
                or column['can_update'] != (pair in BOOKING_UPDATES)
                or column['can_reference'] or column['destructive']):
            raise ValueError('Unexpected effective booking column/table permissions')
    if not (BOOKING_INSERTS | BOOKING_UPDATES) <= seen:
        raise ValueError('Required booking columns are missing')
    return {'result': 'BOOKING_GRANTS_OK', 'identity': ROLE, 'grantee': grantee,
            'read_tables': sorted(BOOKING_READS), 'outbox_read': False,
            'worker_table_access': False, 'destructive_table_access': False}


def run(grant_only=False, seed_only=False, booking_only=False):
    from azure.identity import WorkloadIdentityCredential
    import psycopg
    from psycopg.rows import dict_row

    if os.environ["AZURE_CLIENT_ID"] != CLIENT or os.environ["AZURE_TENANT_ID"] != TENANT:
        raise ValueError("Unexpected injected identity")
    token_file = os.environ["AZURE_FEDERATED_TOKEN_FILE"]
    assertion = Path(token_file).read_text().strip()
    payload = assertion.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("sub") != "system:serviceaccount:trafficxx:migrator":
        raise ValueError("Unexpected service account")
    # Entra validates assertion signature/issuer/audience; local decode is diagnostic.
    ca = "/etc/ssl/certs/ca-certificates.crt"
    if not Path(ca).is_file():
        raise ValueError("Missing public CA bundle")
    folder = Path(__file__).resolve().parent
    # read_text normalizes CRLF, matching the existing additive migration runner.
    payloads = {} if grant_only or seed_only or booking_only else {
        name: (folder / name).read_text(encoding="utf-8") for name in FILES
    }
    with WorkloadIdentityCredential(tenant_id=TENANT, client_id=CLIENT,
                                    token_file_path=token_file) as credential:
        token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
        with psycopg.connect(
            host="trafficxx-pg-e8708a85.postgres.database.azure.com", port=5432,
            dbname="trafficxx", user=ROLE, password=token.token,
            sslmode="verify-full", sslrootcert=ca, connect_timeout=10,
            row_factory=dict_row,
            options="-c search_path=public -c statement_timeout=60000 -c lock_timeout=10000",
        ) as connection:
            session = connection.execute("""
                SELECT current_database() AS database, current_user AS identity,
                       s.ssl, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                       r.rolreplication, r.rolbypassrls,
                       pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
                FROM pg_stat_ssl s JOIN pg_roles r ON r.rolname = current_user
                WHERE s.pid = pg_backend_pid()
            """).fetchone()
            if (not session or session["database"] != "trafficxx" or session["identity"] != ROLE
                    or not session["ssl"] or any(session[key] for key in (
                        "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "admin"))):
                raise ValueError("Unexpected SQL identity, TLS or administrator privileges")
            if booking_only:
                result = grant_booking(connection)
            elif seed_only:
                result = seed_demo(connection)
            elif grant_only:
                result = grant_browse(connection)
            else:
                result = bootstrap(connection, payloads)
        # Only report success after the database transaction has committed.
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--grant-browse", action="store_true",
                         help="Grant browse reads as the table owner, without running migrations")
    actions.add_argument("--seed-demo", action="store_true",
                         help="Add absent demo inventory without resetting existing bookings")
    actions.add_argument("--grant-booking", action="store_true",
                         help="Grant the registered booking role its service SQL permissions")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        run(grant_only=args.grant_browse, seed_only=args.seed_demo, booking_only=args.grant_booking)
    except Exception as error:
        state = getattr(error, "sqlstate", None)
        if not isinstance(state, str) or len(state) != 5 or not state.isalnum():
            state = None
        result = 'BOOKING_GRANTS_FAILED' if args.grant_booking else (
            'DEMO_SEED_FAILED' if args.seed_demo else (
                'BROWSE_GRANTS_FAILED' if args.grant_browse else 'SCHEMA_BOOTSTRAP_FAILED'))
        print(json.dumps({"result": result,
                          "error_type": type(error).__name__, "sqlstate": state}))
        sys.exit(1)
