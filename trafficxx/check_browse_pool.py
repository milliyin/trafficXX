"""User-run isolated Browse-identity pool probe: reads only, no inventory writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os

import psycopg
from fastapi import HTTPException

from trafficxx.browse_db import BrowseDatabase


def run():
    # Only this temporary check process uses one slot, making reuse and capacity
    # observable without borrowing or disrupting the serving Browse pods' pool.
    os.environ['BROWSE_DB_POOL_MAX'] = '1'
    database = BrowseDatabase()
    database.open()
    try:
        database.pool.wait(timeout=30)
        with database.connection() as conn:
            row = conn.execute(
                'SELECT pg_backend_pid() AS pid, current_user AS identity, '
                's.ssl, s.version AS tls_version FROM pg_stat_ssl s WHERE pid = pg_backend_pid()'
            ).fetchone()
            first = row['pid']
        with database.connection() as conn:
            if conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] != first:
                raise RuntimeError('Sequential reuse not observed')

        try:
            with database.connection() as conn:
                conn.execute('SELECT 1 / 0')
        except psycopg.errors.DivisionByZero:
            pass
        else:
            raise RuntimeError('Expected transaction failure not observed')
        with database.connection() as conn:
            if conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] != first:
                raise RuntimeError('Rollback did not retain the working connection')

        def expect_busy():
            try:
                with database.connection():
                    raise RuntimeError('Pool exceeded its connection limit')
            except HTTPException as error:
                if error.status_code != 503 or error.detail != {'code': 'DATABASE_BUSY'}:
                    raise RuntimeError('Unexpected capacity response') from None

        with ThreadPoolExecutor(max_workers=1) as executor:
            with database.connection():
                executor.submit(expect_busy).result(timeout=10)
        with database.connection() as conn:
            conn.close()  # Only our own diagnostic connection; no backend termination.
        with database.connection() as conn:
            if conn.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'] == first:
                raise RuntimeError('Closed connection was not replaced')
        if not row['ssl'] or row['identity'] != 'trafficxx-lab-browse':
            raise RuntimeError('Unexpected identity or TLS')
        print(json.dumps({
            'result': 'BROWSE_POOL_OK', 'identity': row['identity'],
            'ssl': row['ssl'], 'tls_version': row['tls_version'],
            'physical_connection_reused': True, 'rollback_reused_connection': True,
            'capacity_rejected_with_503': True, 'closed_connection_replaced': True,
            'probe_max_connections': 1, 'inventory_writes': False,
            'note': 'Isolated process; serving-pod reuse and token renewal need separate evidence.',
        }))
    finally:
        database.close()


if __name__ == '__main__':
    try:
        run()
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_POOL_FAILED', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
