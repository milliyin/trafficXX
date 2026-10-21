"""Offline checks for privilege-denial handling and savepoint cleanup."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("browse_permissions", ROOT / "scripts/check-browse-permissions.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class SqlError(Exception):
    def __init__(self, state):
        self.sqlstate = state


class Connection:
    def __init__(self, statement=None, state="42501", allowed_forbidden=False):
        self.statements = []
        self.statement = statement
        self.state = state
        self.allowed_forbidden = allowed_forbidden

    def execute(self, statement):
        self.statements.append(statement)
        if statement in probe.DENIED.values() and not self.allowed_forbidden:
            if self.statement is None or statement == self.statement:
                raise SqlError(self.state)
        if statement == self.statement and statement in probe.ALLOWED.values():
            raise SqlError(self.state)
        return SimpleNamespace(fetchall=lambda: [])


class PermissionProbe(unittest.TestCase):
    def test_all_required_reads_and_nine_real_denials_are_reported(self):
        connection = Connection()
        result = probe.verify_permissions(connection)
        self.assertEqual(result["result"], "BROWSE_PERMISSIONS_OK")
        self.assertEqual(len(result["allowed"]), 3)
        self.assertEqual(len(result["denied"]), 9)
        self.assertEqual(connection.statements.count("ROLLBACK TO SAVEPOINT permission_probe"), 9)
        self.assertEqual(connection.statements.count("RELEASE SAVEPOINT permission_probe"), 9)

    def test_unexpected_allowed_write_is_rolled_back_then_rejected(self):
        connection = Connection(allowed_forbidden=True)
        with self.assertRaisesRegex(ValueError, "forbidden operation was allowed"):
            probe.expect_denied(connection, probe.DENIED["insert_event"])
        self.assertEqual(connection.statements[-2:], [
            "ROLLBACK TO SAVEPOINT permission_probe", "RELEASE SAVEPOINT permission_probe"])

    def test_syntax_missing_table_and_read_only_errors_are_not_permission_proof(self):
        for state in ("42601", "42P01", "25006", None):
            with self.subTest(state=state):
                connection = Connection(state=state)
                with self.assertRaises(SqlError):
                    probe.expect_denied(connection, probe.DENIED["insert_event"])
                self.assertEqual(connection.statements[-1], "RELEASE SAVEPOINT permission_probe")

    def test_required_read_failure_stops_before_negative_tests(self):
        connection = Connection(statement=probe.ALLOWED["events"])
        with self.assertRaises(SqlError):
            probe.verify_permissions(connection)
        self.assertFalse(any(statement.startswith("SAVEPOINT") for statement in connection.statements))

    def test_every_negative_case_rejects_unexpected_success(self):
        for name, statement in probe.DENIED.items():
            with self.subTest(name=name):
                connection = Connection(allowed_forbidden=True)
                with self.assertRaises(ValueError):
                    probe.expect_denied(connection, statement)
                self.assertEqual(connection.statements[-1], "RELEASE SAVEPOINT permission_probe")


if __name__ == "__main__":
    unittest.main()
