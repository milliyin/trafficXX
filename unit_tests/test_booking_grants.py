"""Offline effective-privilege policy checks for the booking database role."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('booking_grants', ROOT / 'scripts/bootstrap-azure-schema.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
READS = {'events', 'reservations', 'ticket_units'}
UPDATES = {('events', 'name'), ('reservations', 'status'), ('ticket_units', 'reservation_id')}
INSERTS = {('reservations', c) for c in ('id', 'event_id', 'customer_id', 'idempotency_key',
                                       'request', 'quantity', 'status', 'expires_at')}
INSERTS |= {('outbox', c) for c in ('reservation_id', 'event_type', 'payload')}
FIELDS = {
    'events': ('id', 'name', 'inventory_type'),
    'reservations': ('id', 'event_id', 'customer_id', 'idempotency_key', 'request', 'quantity',
                     'status', 'created_at', 'expires_at'),
    'ticket_units': ('event_id', 'unit_number', 'seat_label', 'reservation_id'),
    'outbox': ('id', 'reservation_id', 'event_type', 'payload', 'published_at', 'publish_attempts'),
    'consumer_inbox': ('message_id',), 'schema_migrations': ('version',),
    'worker_heartbeats': ('name',),
}


class Connection:
    def __init__(self):
        self.owned = [{'relname': name, 'relkind': 'r', 'owner': module.ROLE}
                      for name in module.TABLES]
        self.access = dict(connect=True, usage=True, create_objects=False, create_schema=False,
                           admin=False, rolsuper=False, rolcreatedb=False, rolcreaterole=False,
                           rolreplication=False, rolbypassrls=False)
        self.columns = []
        for table, fields in FIELDS.items():
            for field in fields:
                pair = (table, field)
                self.columns.append(dict(relname=table, attname=field, can_read=table in READS,
                                         can_insert=pair in INSERTS, can_update=pair in UPDATES,
                                         can_reference=False, destructive=False))
        self.grants = []

    def execute(self, sql, params=None):
        if 'has_column_privilege' in sql:
            return SimpleNamespace(fetchall=lambda: self.columns)
        if 'pg_get_userbyid' in sql:
            return SimpleNamespace(fetchall=lambda: self.owned)
        if 'FROM pg_roles WHERE rolname' in sql:
            return SimpleNamespace(fetchone=lambda: self.access)
        if sql.startswith('GRANT'):
            self.grants.append(sql)
        return None

    def column(self, table, field):
        return next(row for row in self.columns if (row['relname'], row['attname']) == (table, field))


class BookingGrants(unittest.TestCase):
    def test_required_contract_is_granted_without_outbox_reads_or_publishing_updates(self):
        connection = Connection()
        result = module.grant_booking(connection)
        self.assertEqual(result['result'], 'BOOKING_GRANTS_OK')
        self.assertFalse(result['outbox_read'])
        self.assertIn('GRANT UPDATE (status) ON public.reservations TO "trafficxx-lab-booking"', connection.grants)
        self.assertIn('GRANT INSERT (reservation_id, event_type, payload) ON public.outbox TO "trafficxx-lab-booking"', connection.grants)
        self.assertFalse(any('ALL PRIVILEGES' in sql or 'ON ALL' in sql or 'SEQUENCE' in sql for sql in connection.grants))

    def test_wrong_owner_aborts_before_grants(self):
        connection = Connection()
        next(row for row in connection.owned if row['relname'] == 'outbox')['owner'] = 'other'
        with self.assertRaisesRegex(ValueError, 'must own'):
            module.grant_booking(connection)
        self.assertEqual(connection.grants, [])

    def test_extra_worker_or_outbox_reads_are_rejected(self):
        for table, column in (('outbox', 'payload'), ('consumer_inbox', 'message_id'), ('schema_migrations', 'version')):
            with self.subTest(table=table):
                connection = Connection()
                connection.column(table, column)['can_read'] = True
                with self.assertRaises(ValueError):
                    module.grant_booking(connection)

    def test_extra_protected_field_updates_are_rejected(self):
        for table, column in (('events', 'inventory_type'), ('reservations', 'customer_id'),
                              ('ticket_units', 'seat_label'), ('outbox', 'published_at')):
            with self.subTest(table=table, column=column):
                connection = Connection()
                connection.column(table, column)['can_update'] = True
                with self.assertRaises(ValueError):
                    module.grant_booking(connection)

    def test_ddl_admin_and_role_creation_are_rejected(self):
        for flag in ('create_objects', 'create_schema', 'admin', 'rolsuper', 'rolcreaterole', 'rolbypassrls'):
            with self.subTest(flag=flag):
                connection = Connection()
                connection.access[flag] = True
                with self.assertRaisesRegex(ValueError, 'role privileges'):
                    module.grant_booking(connection)

    def test_delete_truncate_trigger_or_reference_access_is_rejected(self):
        for flag in ('destructive', 'can_reference'):
            with self.subTest(flag=flag):
                connection = Connection()
                connection.column('reservations', 'id')[flag] = True
                with self.assertRaises(ValueError):
                    module.grant_booking(connection)

    def test_missing_required_grant_is_rejected(self):
        connection = Connection()
        connection.column('reservations', 'status')['can_update'] = False
        with self.assertRaises(ValueError):
            module.grant_booking(connection)

    def test_missing_required_column_is_rejected(self):
        connection = Connection()
        connection.columns.remove(connection.column('reservations', 'request'))
        with self.assertRaisesRegex(ValueError, 'columns are missing'):
            module.grant_booking(connection)


if __name__ == '__main__':
    unittest.main()
