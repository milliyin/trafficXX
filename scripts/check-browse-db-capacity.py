"""Run inside one Browse pod: bounded read-only SQL and numeric plan summaries."""
import json
import logging
import math
import time

from trafficxx.browse_queries import AVAILABILITY_SQL

LIMITS_SQL = (
    "SELECT current_setting('max_connections')::int AS max_connections, "
    "current_setting('superuser_reserved_connections')::int AS superuser_reserved_connections, "
    "current_setting('reserved_connections', true)::int AS reserved_connections"
)
SESSIONS_SQL = (
    "SELECT count(*)::int AS visible_server_sessions, "
    "count(*) FILTER (WHERE datname = current_database())::int AS database_sessions, "
    "count(*) FILTER (WHERE usename = current_user)::int AS browse_sessions, "
    "count(*) FILTER (WHERE datname = current_database() AND state IS NULL)::int AS hidden_database_states "
    "FROM pg_stat_activity"
)


def require(value, code):
    if not value:
        raise ValueError(code)


def plan_summary(document):
    plan = document[0]
    root = plan['Plan']
    values = {'planning_ms': plan['Planning Time'], 'execution_ms': plan['Execution Time'],
              'actual_rows': root['Actual Rows'], 'actual_loops': root['Actual Loops'],
              'shared_hit_blocks': root.get('Shared Hit Blocks', 0),
              'shared_read_blocks': root.get('Shared Read Blocks', 0)}
    require(all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in values.values()), 'INVALID_PLAN_SUMMARY')
    return values


def run(connection_factory=None):
    if connection_factory is None:
        from trafficxx.db import connect
        connection_factory = connect
    with connection_factory() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        conn.execute("SET LOCAL lock_timeout = '500ms'")
        identity = conn.execute(
            "SELECT current_user AS identity, current_database() AS database, "
            "current_setting('transaction_read_only') AS read_only, "
            "s.ssl, s.version AS tls_version FROM pg_stat_ssl s WHERE pid = pg_backend_pid()"
        ).fetchone()
        require(identity is not None and identity['identity'] == 'trafficxx-lab-browse'
                and identity['database'] == 'trafficxx' and identity['read_only'] == 'on'
                and identity['ssl'] is True, 'UNEXPECTED_IDENTITY_OR_READ_ONLY_TLS')
        limits = conn.execute(LIMITS_SQL).fetchone()
        require(limits is not None and all(value is None or (type(value) is int and value >= 0)
                                          for value in limits.values()), 'INVALID_CONNECTION_LIMITS')
        connections = conn.execute(SESSIONS_SQL).fetchone()
        require(connections is not None and all(type(value) is int and value >= 0
                                               for value in connections.values()), 'INVALID_SESSION_COUNTS')
        roundtrips = []
        for _ in range(6):
            begun = time.perf_counter()
            require(conn.execute('SELECT 1 AS n').fetchone()['n'] == 1, 'SIMPLE_READ_FAILED')
            roundtrips.append((time.perf_counter() - begun) * 1000)
        plans = []
        for event in ('demo-general', 'demo-seated'):
            begun = time.perf_counter()
            row = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, TIMING OFF) ' + AVAILABILITY_SQL,
                               (event,)).fetchone()
            summary = plan_summary(row['QUERY PLAN'])
            require(summary['actual_rows'] == 1, 'DEMO_EVENT_NOT_FOUND')
            plans.append({'event': event, **summary,
                          'client_explain_ms': round((time.perf_counter() - begun) * 1000, 3)})
    return {'result': 'BROWSE_DB_CAPACITY_SQL_OK', 'identity': identity['identity'],
            'database': identity['database'], 'ssl': True, 'tls_version': identity['tls_version'],
            'transaction_read_only': True, 'statement_timeout_ms': 2000, 'lock_timeout_ms': 500,
            'limits': dict(limits), 'sessions': dict(connections),
            'simple_select_ms': {'samples': 6, 'min': round(min(roundtrips), 3),
                                 'mean': round(sum(roundtrips) / len(roundtrips), 3),
                                 'max': round(max(roundtrips), 3)},
            'plans': plans, 'inventory_writes': False,
            'note': 'One separate diagnostic connection, now closed; counts include it. '
                    'Catalog visibility is limited by the Browse role. EXPLAIN runs two bounded SELECTs; '
                    'server execution is not pure CPU time. No database capacity or saturation verdict.'}


if __name__ == '__main__':
    logging.getLogger('azure').setLevel(logging.WARNING)
    try:
        print(json.dumps(run()))
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_DB_CAPACITY_SQL_FAILED',
                          'error_type': type(error).__name__, 'sqlstate': getattr(error, 'sqlstate', None)}))
        raise SystemExit(1) from None
