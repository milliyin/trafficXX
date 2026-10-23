"""Isolated AKS check: real SDK expiry boundary and new SQL sessions, no tokens logged."""
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time
from uuid import uuid4


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def require(condition, check):
    if not condition:
        raise RenewalFailure(check)


class RenewalFailure(RuntimeError):
    pass


class ObservedCredential:
    """Delegate unchanged calls to ONE real SDK object; retain only safe metadata."""
    def __init__(self, real, clock=time.time):
        self.real, self.clock = real, clock
        self.lock = threading.Lock()
        self.first_digest = None
        self.latest = None

    def get_token(self, *args, **kwargs):
        access = self.real.get_token(*args, **kwargs)
        require(type(access.expires_on) is int and access.expires_on > 0,
                'SDK_EXPIRY_METADATA_INVALID')
        digest = hashlib.sha256(access.token.encode()).digest()
        with self.lock:
            if self.first_digest is None:
                self.first_digest = digest
            self.latest = {'expires_on': access.expires_on, 'observed_at': self.clock(),
                           'token_changed': digest != self.first_digest}
        return access

    def metadata(self):
        with self.lock:
            require(self.latest is not None, 'NO_TOKEN_OBSERVATION')
            return dict(self.latest)

    def close(self):
        return self.real.close()


def exercise(probe, emit, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
    started = monotonic()
    run_id = uuid4().hex
    first = probe()
    first_expiry = first['expires_on']
    target = first_expiry + 30
    require(first_expiry > first['server_epoch'], 'INITIAL_TOKEN_ALREADY_EXPIRED')
    require(abs(clock() - first['server_epoch']) <= 120, 'CLOCK_SKEW_TOO_LARGE')
    require(target - clock() <= 90000 - 300, 'EXPIRY_OUTSIDE_25_HOUR_TEST_WINDOW')
    emit({'result': 'TOKEN_RENEWAL_WAITING', 'run_id': run_id,
          'initial_token_expires_utc': utc(first_expiry),
          'earliest_final_check_utc': utc(target),
          'estimated_wait_seconds': max(0, math.ceil(target - clock())),
          'probe_interval_seconds': 300, 'expiry_source': 'SDK_AccessToken.expires_on',
          'note': 'Background Job; Cloud Shell can disconnect. No token or digest is printed.'})
    checks = 1
    while True:
        require(monotonic() - started < 90000, 'TEST_DEADLINE_EXCEEDED')
        sleep(min(300, max(0, target - clock())))
        require(monotonic() - started < 90000, 'TEST_DEADLINE_EXCEEDED')
        current = probe()  # Closes only this Job's own prior session before borrowing again.
        checks += 1
        require(current['expires_on'] > current['server_epoch'], 'EXPIRED_TOKEN_METADATA_AT_SQL_CHECK')
        if clock() >= target:
            require(current['server_epoch'] >= target, 'SERVER_EXPIRY_BOUNDARY_NOT_REACHED')
            require(current['observed_at'] >= target, 'NEW_LOGIN_NOT_OBSERVED_AFTER_BOUNDARY')
            require(current['expires_on'] > first_expiry and current['token_changed'],
                    'TOKEN_REPLACEMENT_NOT_OBSERVED')
            result = {'result': 'DATABASE_TOKEN_RENEWAL_OK', 'run_id': run_id,
                      'initial_token_expires_utc': utc(first_expiry),
                      'replacement_token_expires_utc': utc(current['expires_on']),
                      'final_sql_check_utc': utc(current['server_epoch']),
                      'elapsed_seconds': round(monotonic() - started, 1), 'sql_checks': checks,
                      'token_changed': True, 'new_sql_login_after_initial_expiry': True,
                      'same_sdk_credential_instance': True, 'database': current['database'],
                      'identity': current['identity'], 'ssl': current['ssl'],
                      'tls_version': current['tls_version'], 'inventory_writes': False,
                      'note': 'Isolated process; does not prove uninterrupted serving-pod availability.'}
            emit(result)
            return result
        emit({'result': 'TOKEN_RENEWAL_PROGRESS', 'run_id': run_id, 'sql_checks': checks,
              'last_sql_check_utc': utc(current['server_epoch']),
              'token_expires_utc': utc(current['expires_on']),
              'remaining_seconds': max(0, math.ceil(target - clock()))})


def real_probe():
    import os
    import trafficxx.db as db
    from trafficxx.browse_db import BrowseDatabase

    require(os.environ.get('DATABASE_AUTH_MODE') == 'workload_identity', 'WORKLOAD_IDENTITY_REQUIRED')
    settings = tuple(db._required(name) for name in (
        'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_FEDERATED_TOKEN_FILE'))
    real = db._workload_credential(settings)
    observed = ObservedCredential(real)
    # This ONLY instruments the diagnostic process. Application pods are not
    # touched. All acquisition/cache/network work stays with the original SDK
    # instance; no cache clearing, fake expiry or alternate authentication.
    db._credential = observed
    os.environ['BROWSE_DB_POOL_MAX'] = '1'
    database = BrowseDatabase()
    previous_pid = None

    def probe():
        nonlocal previous_pid
        require(db._credential is observed and observed.real is real, 'SDK_CREDENTIAL_INSTANCE_CHANGED')
        if previous_pid is not None:
            with database.connection() as conn:
                conn.close()
            # Give background token acquisition and TLS login a diagnostic
            # budget without changing the serving pool's three-second timeout.
            database.pool.wait(timeout=60)
        with database.connection() as conn:
            row = conn.execute(
                'SELECT pg_backend_pid() AS pid, current_database() AS database, '
                'current_user AS identity, s.ssl, s.version AS tls_version, '
                'extract(epoch FROM clock_timestamp())::double precision AS server_epoch '
                'FROM pg_stat_ssl s WHERE pid = pg_backend_pid()'
            ).fetchone()
        require(row is not None and row['identity'] == 'trafficxx-lab-browse'
                and row['database'] == 'trafficxx' and row['ssl'] is True,
                'UNEXPECTED_DATABASE_IDENTITY_OR_TLS')
        require(previous_pid is None or row['pid'] != previous_pid, 'PHYSICAL_SQL_SESSION_NOT_REPLACED')
        previous_pid = row.pop('pid')
        return {**row, **observed.metadata()}

    return database, probe


def main():
    import logging
    logging.getLogger('azure').setLevel(logging.CRITICAL)
    logging.getLogger('psycopg.pool').setLevel(logging.CRITICAL)
    database = None
    try:
        database, probe = real_probe()
        database.open()
        database.pool.wait(timeout=60)
        def progress(result):
            if result['result'] != 'DATABASE_TOKEN_RENEWAL_OK':
                print(json.dumps(result), flush=True)

        result = exercise(probe, progress)
        database.close()
        database = None
        print(json.dumps(result), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'result': 'DATABASE_TOKEN_RENEWAL_FAILED',
                          'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, RenewalFailure) else None,
                          'note': 'No tokens or fingerprints printed; inspect Job status, do not reset inventory.'}), flush=True)
        return 1
    finally:
        if database is not None:
            try:
                database.close()
            except Exception:
                pass  # Preserve the original safe failure result.


if __name__ == '__main__':
    raise SystemExit(main())
