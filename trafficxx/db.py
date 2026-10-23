import atexit
import os
from pathlib import Path
from threading import Lock

import psycopg
from psycopg.rows import dict_row

_credential = None
_credential_settings = None
_credential_lock = Lock()
_postgres_scope = 'https://ossrdbms-aad.database.windows.net/.default'


def _required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise psycopg.OperationalError(f'Missing required database setting: {name}')
    return value


def _workload_credential(settings):
    global _credential, _credential_settings
    with _credential_lock:
        if _credential is None:
            from azure.identity import WorkloadIdentityCredential

            tenant, client, token_file = settings
            _credential = WorkloadIdentityCredential(
                tenant_id=tenant, client_id=client, token_file_path=token_file,
            )
            _credential_settings = settings
            atexit.register(_credential.close)
        elif _credential_settings != settings:
            raise psycopg.OperationalError('Workload identity settings changed; restart the process')
        return _credential


def connection_parameters():
    common = dict(row_factory=dict_row, connect_timeout=5,
                  options='-c statement_timeout=15000 -c lock_timeout=10000')
    mode = os.environ.get('DATABASE_AUTH_MODE', 'url')
    if mode == 'url':
        return dict(conninfo=os.environ['DATABASE_URL'], **common)
    if mode != 'workload_identity':
        raise psycopg.OperationalError('Unsupported DATABASE_AUTH_MODE')
    if os.environ.get('DATABASE_URL'):
        raise psycopg.OperationalError('Remove DATABASE_URL when using workload identity')

    host, database, user = (_required(name) for name in ('PGHOST', 'PGDATABASE', 'PGUSER'))
    settings = tuple(_required(name) for name in (
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_FEDERATED_TOKEN_FILE',
    ))
    ca = os.environ.get('PGSSLROOTCERT', '/etc/ssl/certs/ca-certificates.crt')
    if not Path(ca).is_file() or not os.access(ca, os.R_OK):
        raise psycopg.OperationalError('Missing readable PostgreSQL CA trust bundle')
    if not Path(settings[2]).is_file() or not os.access(settings[2], os.R_OK):
        raise psycopg.OperationalError('Missing readable projected workload token file')
    try:
        port = int(os.environ.get('PGPORT', '5432'))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise psycopg.OperationalError('Invalid PostgreSQL port') from None

    try:
        # Reuse the SDK's in-memory cache; request a usable token for EVERY new
        # database connection so an expired startup token is never pinned here.
        token = _workload_credential(settings).get_token(_postgres_scope).token
        if not isinstance(token, str) or not token:
            raise ValueError('No access token returned')
    except Exception:
        # Readiness already handles psycopg errors as 503. Do not expose raw
        # identity-service error bodies or fall back to a human/CLI credential.
        raise psycopg.OperationalError('Workload identity database authentication failed') from None
    return dict(host=host, port=port, dbname=database, user=user,
                password=token, sslmode='verify-full', sslrootcert=ca, **common)


def connect():
    parameters = connection_parameters()
    if 'conninfo' in parameters:
        return psycopg.connect(parameters.pop('conninfo'), **parameters)
    return psycopg.connect(**parameters)
