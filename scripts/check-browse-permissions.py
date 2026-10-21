"""Read and negative-permission checks using only the browse workload identity."""
import base64
import json
import logging
import os
from pathlib import Path
import sys
from uuid import uuid4

CLIENT = "851cbe64-04d0-416a-8ebb-7c43b9e18aa5"
TENANT = "d2828580-3217-43ad-9f3f-8d430998b86e"
ROLE = "trafficxx-lab-browse"
ALLOWED = {
    "events": "SELECT * FROM public.events ORDER BY id LIMIT 1",
    "readiness": "SELECT 1 FROM public.events LIMIT 1",
    "availability": """
        SELECT u.seat_label FROM public.ticket_units u
        LEFT JOIN public.reservations r ON r.id = u.reservation_id
        WHERE u.event_id = '__trafficxx_permission_probe__'
          AND (u.reservation_id IS NULL OR
               (r.status = 'HELD' AND r.expires_at <= clock_timestamp()))
        ORDER BY u.unit_number
    """,
}
DENIED = {
    "insert_event": f"""
        INSERT INTO public.events(id, name, inventory_type)
        VALUES ('permission-probe-{uuid4().hex}', 'Permission probe', 'GENERAL')
    """,
    "update_event": "UPDATE public.events SET name = name WHERE false",
    "delete_event": "DELETE FROM public.events WHERE false",
    "create_table": "CREATE TABLE public.trafficxx_browse_permission_probe (id integer)",
    "create_schema": "CREATE SCHEMA trafficxx_browse_permission_probe",
    "customer_details": "SELECT customer_id FROM public.reservations LIMIT 0",
    "full_reservations": "SELECT * FROM public.reservations LIMIT 0",
    "outbox": "SELECT * FROM public.outbox LIMIT 0",
    "migration_ledger": "SELECT * FROM public.schema_migrations LIMIT 0",
}


def expect_denied(connection, statement):
    connection.execute("SAVEPOINT permission_probe")
    try:
        connection.execute(statement)
    except Exception as error:
        connection.execute("ROLLBACK TO SAVEPOINT permission_probe")
        connection.execute("RELEASE SAVEPOINT permission_probe")
        # A syntax error, missing table or read-only transaction is not evidence
        # of a permission denial. Require PostgreSQL insufficient_privilege.
        if getattr(error, "sqlstate", None) != "42501":
            raise
    else:
        connection.execute("ROLLBACK TO SAVEPOINT permission_probe")
        connection.execute("RELEASE SAVEPOINT permission_probe")
        raise ValueError("A forbidden operation was allowed; its changes were rolled back")


def verify_permissions(connection):
    for statement in ALLOWED.values():
        connection.execute(statement).fetchall()
    for statement in DENIED.values():
        expect_denied(connection, statement)
    return {"result": "BROWSE_PERMISSIONS_OK", "identity": ROLE,
            "allowed": list(ALLOWED), "denied": list(DENIED), "denial_sqlstate": "42501"}


def run():
    from azure.identity import WorkloadIdentityCredential
    import psycopg

    if os.environ["AZURE_CLIENT_ID"] != CLIENT or os.environ["AZURE_TENANT_ID"] != TENANT:
        raise ValueError("Unexpected injected identity")
    token_file = os.environ["AZURE_FEDERATED_TOKEN_FILE"]
    assertion = Path(token_file).read_text().strip()
    payload = assertion.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("sub") != "system:serviceaccount:trafficxx:browse":
        raise ValueError("Unexpected service account")
    ca = "/etc/ssl/certs/ca-certificates.crt"
    if not Path(ca).is_file():
        raise ValueError("Missing public CA bundle")
    with WorkloadIdentityCredential(tenant_id=TENANT, client_id=CLIENT,
                                    token_file_path=token_file) as credential:
        token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
        with psycopg.connect(
            host="trafficxx-pg-e8708a85.postgres.database.azure.com", port=5432,
            dbname="trafficxx", user=ROLE, password=token.token,
            sslmode="verify-full", sslrootcert=ca, connect_timeout=10,
            options="-c search_path=public -c statement_timeout=15000 -c lock_timeout=5000",
        ) as connection:
            # Explicitly read-write: a read-only error must never stand in for
            # an actual privilege rejection in the negative tests.
            connection.execute("SET TRANSACTION READ WRITE")
            row = connection.execute("""
                SELECT current_database(), current_user, s.ssl, s.version,
                       r.rolsuper, r.rolcreatedb, r.rolcreaterole,
                       r.rolreplication, r.rolbypassrls,
                       pg_has_role(current_user, 'azure_pg_admin', 'MEMBER')
                FROM pg_stat_ssl s JOIN pg_roles r ON r.rolname = current_user
                WHERE s.pid = pg_backend_pid()
            """).fetchone()
            if not row or row[0] != "trafficxx" or row[1] != ROLE or not row[2] or any(row[4:]):
                raise ValueError("Unexpected SQL identity, TLS or administrator privileges")
            result = verify_permissions(connection)
            result.update({"database": row[0], "ssl": row[2], "tls_version": row[3], "admin": False})
            # Success is read-only in effect; never commit diagnostic changes.
            connection.rollback()
    print(json.dumps(result))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        run()
    except Exception as error:
        state = getattr(error, "sqlstate", None)
        if not isinstance(state, str) or len(state) != 5 or not state.isalnum():
            state = None
        print(json.dumps({"result": "BROWSE_PERMISSIONS_FAILED",
                          "error_type": type(error).__name__, "sqlstate": state}))
        sys.exit(1)
