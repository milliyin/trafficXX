"""Offline migration decisions only; these checks never connect to a database."""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("azure_bootstrap", ROOT / "scripts/bootstrap-azure-schema.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
PAYLOADS = {
    "001-schema.sql": (ROOT / "database/001-schema.sql").read_text(encoding="utf-8"),
    "002-messaging.sql": (ROOT / "database/migrations/002-messaging.sql").read_text(encoding="utf-8"),
}
BASE_TABLES = {"events", "reservations", "ticket_units", "outbox"}


class FakeConnection:
    def __init__(self):
        self.objects = {}
        self.ledger = {}
        self.executed = []
        self.fail_on = None
        self.access = {"connect": True, "usage": True, "create_objects": False,
                       "create_schema": False, "admin": False, "writes": False,
                       "private_reservations": False}

    def add(self, names, owner=bootstrap.ROLE):
        for name in names:
            self.objects[name] = {"relname": name, "relkind": "r", "owner": owner}

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql == self.fail_on:
            raise RuntimeError("Simulated migration failure")
        if "FROM pg_class" in sql:
            return SimpleNamespace(fetchall=lambda: list(self.objects.values()))
        if "CREATE TABLE public.schema_migrations" in sql:
            self.add({"schema_migrations"})
        elif "SELECT version, checksum" in sql:
            return SimpleNamespace(fetchall=lambda: [
                {"version": version, "checksum": checksum} for version, checksum in self.ledger.items()
            ])
        elif sql.startswith("INSERT INTO public.schema_migrations"):
            self.ledger[params[0]] = params[1]
        elif sql == PAYLOADS["001-schema.sql"]:
            self.add(BASE_TABLES)
        elif sql == PAYLOADS["002-messaging.sql"]:
            self.add(bootstrap.TABLES - BASE_TABLES - {"schema_migrations"})
        elif "AS private_reservations" in sql:
            return SimpleNamespace(fetchone=lambda: self.access)
        return SimpleNamespace(fetchall=lambda: [])


class BootstrapDecisions(unittest.TestCase):
    def populated(self):
        connection = FakeConnection()
        bootstrap.bootstrap(connection, PAYLOADS)
        connection.executed.clear()
        return connection

    def test_empty_schema_applies_base_then_messaging_and_records_checksums(self):
        connection = FakeConnection()
        result = bootstrap.bootstrap(connection, PAYLOADS)
        self.assertEqual(result["applied"], list(bootstrap.FILES))
        self.assertEqual(result["verified_tables"], 10)
        for name in bootstrap.FILES:
            self.assertEqual(connection.ledger[name], hashlib.sha256(PAYLOADS[name].encode()).hexdigest())
        self.assertLess(connection.executed.index(PAYLOADS["001-schema.sql"]),
                        connection.executed.index(PAYLOADS["002-messaging.sql"]))

    def test_repeat_skips_both_scripts_without_replaying_ddl(self):
        connection = self.populated()
        result = bootstrap.bootstrap(connection, PAYLOADS)
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["skipped"], list(bootstrap.FILES))
        self.assertFalse(any(payload in connection.executed for payload in PAYLOADS.values()))

    def test_edited_applied_file_is_rejected_before_any_migration(self):
        connection = self.populated()
        edited = dict(PAYLOADS, **{"002-messaging.sql": PAYLOADS["002-messaging.sql"] + "\n-- changed"})
        with self.assertRaisesRegex(ValueError, "edited"):
            bootstrap.bootstrap(connection, edited)
        self.assertFalse(any(payload in connection.executed for payload in edited.values()))

    def test_existing_untracked_schema_is_not_adopted(self):
        connection = FakeConnection()
        connection.add({"events"})
        with self.assertRaisesRegex(ValueError, "untracked"):
            bootstrap.bootstrap(connection, PAYLOADS)
        self.assertNotIn("schema_migrations", connection.objects)

    def test_existing_ledger_owned_by_someone_else_is_not_adopted(self):
        connection = FakeConnection()
        connection.add({"schema_migrations"}, owner="another-user")
        with self.assertRaisesRegex(ValueError, "ledger owner"):
            bootstrap.bootstrap(connection, PAYLOADS)

    def test_tables_without_base_record_are_rejected(self):
        connection = FakeConnection()
        connection.add({"schema_migrations", "events"})
        with self.assertRaisesRegex(ValueError, "no bootstrap record"):
            bootstrap.bootstrap(connection, PAYLOADS)

    def test_tracked_base_can_resume_with_only_messaging_pending(self):
        connection = FakeConnection()
        connection.add(BASE_TABLES | {"schema_migrations"})
        connection.ledger["001-schema.sql"] = hashlib.sha256(PAYLOADS["001-schema.sql"].encode()).hexdigest()
        result = bootstrap.bootstrap(connection, PAYLOADS)
        self.assertEqual(result["applied"], ["002-messaging.sql"])
        self.assertEqual(result["skipped"], ["001-schema.sql"])

    def test_missing_table_is_rejected_even_when_ledger_says_applied(self):
        connection = self.populated()
        del connection.objects["events"]
        with self.assertRaisesRegex(ValueError, "Missing application table"):
            bootstrap.bootstrap(connection, PAYLOADS)

    def test_wrong_application_table_owner_is_rejected(self):
        connection = self.populated()
        connection.objects["events"]["owner"] = "another-user"
        with self.assertRaisesRegex(ValueError, "unexpected table ownership"):
            bootstrap.bootstrap(connection, PAYLOADS)

    def test_migration_error_propagates_to_transaction_owner(self):
        connection = FakeConnection()
        connection.fail_on = PAYLOADS["002-messaging.sql"]
        with self.assertRaisesRegex(RuntimeError, "Simulated migration failure"):
            bootstrap.bootstrap(connection, PAYLOADS)

    def test_browse_grants_only_required_reservation_columns(self):
        connection = self.populated()
        result = bootstrap.grant_browse(connection)
        self.assertEqual(result["result"], "BROWSE_GRANTS_OK")
        grants = [sql for sql in connection.executed if sql.startswith("GRANT")]
        self.assertEqual(len(grants), 3)
        self.assertIn('GRANT SELECT (id, status, expires_at) ON public.reservations TO "trafficxx-lab-browse"', grants)
        self.assertFalse(any("customer_id" in sql or "ALL PRIVILEGES" in sql for sql in grants))

    def test_browse_grants_reject_foreign_table_owner_before_granting(self):
        connection = self.populated()
        connection.objects["events"]["owner"] = "someone-else"
        with self.assertRaisesRegex(ValueError, "must own"):
            bootstrap.grant_browse(connection)
        self.assertFalse(any(sql.startswith("GRANT") for sql in connection.executed))

    def test_browse_grants_reject_effective_write_create_admin_or_private_reads(self):
        for flag in ("create_objects", "create_schema", "admin", "writes", "private_reservations"):
            with self.subTest(flag=flag):
                connection = self.populated()
                connection.access[flag] = True
                with self.assertRaisesRegex(ValueError, "effective browse permissions"):
                    bootstrap.grant_browse(connection)

    def test_browse_grants_require_existing_connect_and_schema_usage(self):
        for flag in ("connect", "usage"):
            with self.subTest(flag=flag):
                connection = self.populated()
                connection.access[flag] = False
                with self.assertRaisesRegex(ValueError, "effective browse permissions"):
                    bootstrap.grant_browse(connection)


if __name__ == "__main__":
    unittest.main()
