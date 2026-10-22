"""Offline safeguards for the duplicate experiment's read-only baseline."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    'duplicate_baseline', Path(__file__).resolve().parents[1] / 'scripts/observe-duplicate-baseline.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Connection:
    def __init__(self):
        self.statements = []
        self.session = dict(identity='trafficxx-lab-migrator', database='trafficxx',
                            ssl=True, version='TLSv1.3', admin=False)
        self.target = dict(id=3, message_id='stable-id', published=True, processed=True)
        self.contents = '[{"envelope":"PRIVATE_BODY","updated_at":"2026-10-23"}]'
        self.count = 12

    def execute(self, sql):
        self.statements.append(sql)
        row = self.session if 'pg_stat_ssl' in sql else self.target if 'WHERE o.id = 3' in sql else {
            'count': self.count, 'contents': self.contents}
        return SimpleNamespace(fetchone=lambda: row)


class DuplicateBaseline(unittest.TestCase):
    def test_read_only_fingerprints_exclude_raw_contents(self):
        conn = Connection()
        result = module.snapshot(conn)
        self.assertEqual(result['result'], 'DUPLICATE_BASELINE_OK')
        self.assertEqual(result['outbox_id'], 3)
        self.assertEqual(set(result['tables']), {'consumer_inbox', 'reservation_updates', 'outbox'})
        self.assertTrue(conn.statements[0].endswith('READ ONLY'))
        self.assertTrue(all(s.lstrip().startswith('SELECT') for s in conn.statements[1:]))
        self.assertNotIn('PRIVATE_BODY', json.dumps(result))

    def test_stable_state_has_stable_fingerprint(self):
        self.assertEqual(module.snapshot(Connection())['tables'], module.snapshot(Connection())['tables'])

    def test_timestamp_only_change_changes_fingerprint(self):
        conn = Connection()
        original = module.snapshot(conn)['tables']
        conn.contents = conn.contents.replace('2026-10-23', '2026-10-24')
        self.assertNotEqual(original, module.snapshot(conn)['tables'])

    def test_wrong_identity_tls_or_admin_fails_before_table_reads(self):
        for change in ({'identity': 'trafficxx-lab-publisher'}, {'ssl': False}, {'admin': True}):
            with self.subTest(change=change):
                conn = Connection()
                conn.session.update(change)
                with self.assertRaises(ValueError):
                    module.snapshot(conn)
                self.assertEqual(len(conn.statements), 2)

    def test_missing_unpublished_or_unprocessed_target_fails(self):
        for target in (None, dict(published=False, processed=True), dict(published=True, processed=False)):
            with self.subTest(target=target):
                conn = Connection()
                conn.target = target
                with self.assertRaises(ValueError):
                    module.snapshot(conn)

    def test_empty_tables_fail_instead_of_recording_empty_baseline(self):
        conn = Connection()
        conn.count = 0
        with self.assertRaises(ValueError):
            module.snapshot(conn)
