"""Read-only retained-audit and resumed-valid-work diagnostic decisions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    'poison_recovery', Path(__file__).resolve().parents[1] / 'scripts/check-poison-recovery.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Connection:
    def __init__(self):
        self.statements = []
        self.rows = [dict(queue_name='reservation-events', sequence_number=16, reason='INVALID_EVENT_SCHEMA')]
        self.inbox_count = 0

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return SimpleNamespace(fetchall=lambda: self.rows, fetchone=lambda: {'count': self.inbox_count})


GOOD = dict(result='CONSUMER_DRAIN_OK', total_count=16, inbox_count=16, expected_projection_count=8)


class PoisonRecovery(unittest.TestCase):
    def test_success_is_read_only_and_keeps_audit(self):
        conn = Connection()
        result = module.verify(conn, GOOD)
        self.assertEqual(result['result'], 'POISON_RECOVERY_OK')
        self.assertTrue(result['audit_record_retained'])
        self.assertEqual(conn.statements[0], 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertTrue(all(s.startswith('SELECT') for s in conn.statements[1:]))

    def test_missing_wrong_or_duplicate_audit_fails(self):
        for rows in ([], [dict(queue_name='other', sequence_number=16, reason='INVALID_EVENT_SCHEMA')],
                     [dict(queue_name='reservation-events', sequence_number=15, reason='INVALID_EVENT_SCHEMA')],
                     [dict(queue_name='reservation-events', sequence_number=16, reason='OTHER')],
                     Connection().rows * 2):
            with self.subTest(rows=rows):
                conn = Connection()
                conn.rows = rows
                with self.assertRaises(ValueError):
                    module.verify(conn, GOOD)

    def test_expected_new_valid_flow_must_have_arrived(self):
        for change in ({'result': 'FAILED'}, {'total_count': 14}, {'inbox_count': 14},
                       {'expected_projection_count': 7}):
            with self.subTest(change=change):
                conn = Connection()
                with self.assertRaises(ValueError):
                    module.verify(conn, {**GOOD, **change})
                self.assertFalse(conn.statements)

    def test_poison_inbox_row_fails(self):
        conn = Connection()
        conn.inbox_count = 1
        with self.assertRaises(ValueError):
            module.verify(conn, GOOD)
