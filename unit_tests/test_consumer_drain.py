"""Offline read-only observation and bounded drain-wait decisions."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    'consumer_drain', Path(__file__).resolve().parents[1] / 'scripts/check-consumer-drain.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Connection:
    def __init__(self):
        self.statements = []
        self.session = dict(identity='trafficxx-lab-migrator', database='trafficxx', ssl=True,
                            version='TLSv1.3', admin=False)
        self.counts = dict(total_count=12, pending_count=0, inbox_count=12, missing_inbox_count=0,
                           payload_mismatch_count=0, expected_projection_count=6,
                           projection_mismatch_count=0, orphan_inbox_count=0,
                           orphan_projection_count=0, failure_record_count=0,
                           observed_dead_letter_count=0)
        self.beat = dict(age_seconds=1.5, last_error=None)

    def execute(self, sql):
        self.statements.append(sql.strip())
        row = self.session if 'FROM pg_stat_ssl' in sql else (
            self.beat if 'FROM public.worker_heartbeats' in sql else self.counts)
        return SimpleNamespace(fetchone=lambda: row)


class ConsumerDrain(unittest.TestCase):
    def test_snapshot_is_read_only_and_reports_counts_and_heartbeat(self):
        conn = Connection()
        state = module.snapshot(conn)
        self.assertTrue(module.drained(state))
        self.assertEqual(state['inbox_count'], 12)
        self.assertEqual(conn.statements[0], 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertTrue(all(sql.startswith(('SELECT', 'WITH')) for sql in conn.statements[1:]))

    def test_pending_empty_stale_missing_or_error_heartbeat_cannot_pass(self):
        good = module.snapshot(Connection())
        changes = [dict(pending_count=1), dict(total_count=0, inbox_count=0),
                   dict(inbox_count=11), dict(missing_inbox_count=1), dict(payload_mismatch_count=1),
                   dict(expected_projection_count=0), dict(projection_mismatch_count=1),
                   dict(orphan_inbox_count=1), dict(orphan_projection_count=1),
                   dict(heartbeat_age_seconds=31), dict(heartbeat_age_seconds=-1),
                   dict(heartbeat_present=False), dict(heartbeat_has_error=True),
                   dict(heartbeat_age_seconds=None)]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(module.drained({**good, **change}))

    def test_historical_retry_failures_do_not_hide_successful_drain(self):
        conn = Connection()
        conn.counts['failure_record_count'] = 3
        self.assertTrue(module.drained(module.snapshot(conn)))

    def test_missing_heartbeat_is_explicit_and_raw_error_not_printed(self):
        conn = Connection()
        conn.beat = None
        state = module.snapshot(conn)
        self.assertFalse(state['heartbeat_present'])
        self.assertIsNone(state['heartbeat_age_seconds'])
        self.assertFalse(module.drained(state))
        conn.beat = dict(age_seconds=1, last_error='DO_NOT_LOG_RAW')
        self.assertNotIn('DO_NOT_LOG_RAW', json.dumps(module.snapshot(conn)))

    def test_wrong_identity_or_admin_fails_before_reading_outbox(self):
        for change in (dict(identity='trafficxx-lab-consumer'), dict(admin=True), dict(ssl=False)):
            with self.subTest(change=change):
                conn = Connection()
                conn.session.update(change)
                with self.assertRaises(ValueError):
                    module.snapshot(conn)
                self.assertEqual(len(conn.statements), 2)

    def scenario(self, mode='success'):
        database = types.ModuleType('trafficxx.db')
        conn = MagicMock()
        conn.__enter__.return_value = conn
        database.connect = MagicMock(return_value=conn)
        good = module.snapshot(Connection())
        pending = {**good, 'missing_inbox_count': 1, 'inbox_count': 11}
        states = [pending, good] if mode == 'success' else None
        env = dict(AZURE_CLIENT_ID='wrong' if mode == 'wrong-client' else module.CLIENT,
                   AZURE_TENANT_ID=module.TENANT, DATABASE_AUTH_MODE='workload_identity',
                   PGUSER='trafficxx-lab-migrator', PGDATABASE='trafficxx',
                   PGHOST='trafficxx-pg-e8708a85.postgres.database.azure.com')
        clock = [0]

        def sleep(seconds):
            clock[0] += seconds

        output = io.StringIO()
        with patch.dict('sys.modules', {'trafficxx.db': database}), \
                patch.dict(os.environ, env, clear=True), \
                patch.object(module, 'snapshot', side_effect=states,
                             return_value=pending), \
                patch.object(module.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(module.time, 'sleep', side_effect=sleep), \
                patch.object(module.logging, 'disable'), contextlib.redirect_stdout(output):
            status = module.main()
        return status, json.loads(output.getvalue()), clock, database

    def test_bounded_wait_observes_drain_and_fresh_heartbeat(self):
        status, result, clock, database = self.scenario()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'CONSUMER_DRAIN_OK')
        self.assertEqual(clock[0], 5)
        self.assertEqual(database.connect.call_count, 2)

    def test_timeout_reports_last_snapshot_without_writes(self):
        status, result, clock, _ = self.scenario('timeout')
        self.assertEqual(status, 1)
        self.assertEqual(clock[0], 120)
        self.assertEqual(result['snapshot']['missing_inbox_count'], 1)
        self.assertEqual(result['error_type'], 'TimeoutError')

    def test_wrong_client_never_opens_sql(self):
        status, result, _, database = self.scenario('wrong-client')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'preflight')
        database.connect.assert_not_called()
