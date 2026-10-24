"""Execute capacity diagnostic SQL on the isolated CI PostgreSQL database."""
import importlib.util
from pathlib import Path

from trafficxx.browse_queries import AVAILABILITY_SQL
from trafficxx.db import connect


def test_capacity_queries_and_plan_summary_are_read_only():
    spec = importlib.util.spec_from_file_location('capacity_sql',
        Path(__file__).resolve().parents[1] / 'scripts/check-browse-db-capacity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        conn.execute("SET LOCAL lock_timeout = '500ms'")
        limits = conn.execute(module.LIMITS_SQL).fetchone()
        sessions = conn.execute(module.SESSIONS_SQL).fetchone()
        assert limits['max_connections'] > 0
        assert all(value >= 0 for value in sessions.values())
        plan = conn.execute('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON, TIMING OFF) ' + AVAILABILITY_SQL,
                            ('seated',)).fetchone()['QUERY PLAN']
        summary = module.plan_summary(plan)
        assert summary['actual_rows'] == 1
        assert summary['execution_ms'] >= 0
        assert conn.execute("SHOW transaction_read_only").fetchone()['transaction_read_only'] == 'on'
