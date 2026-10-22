"""Effective SQL access boundary for the background expiry worker."""
import unittest
from test_booking_grants import Connection, module


READS = {'events', 'reservations'}
READ_COLUMNS = {('ticket_units', 'reservation_id')}
INSERTS = {('outbox', c) for c in ('reservation_id', 'event_type', 'payload')}
UPDATES = {('events', 'name'), ('reservations', 'status'), ('ticket_units', 'reservation_id')}


def connection():
    conn = Connection()
    for row in conn.columns:
        pair = (row['relname'], row['attname'])
        row.update(can_read=pair[0] in READS or pair in READ_COLUMNS,
                   can_insert=pair in INSERTS, can_update=pair in UPDATES)
    return conn


class ExpiryGrants(unittest.TestCase):
    def test_sweep_contract_has_no_reservation_insert_or_outbox_read(self):
        conn = connection()
        result = module.grant_expiry(conn)
        self.assertEqual(result['result'], 'EXPIRY_GRANTS_OK')
        self.assertIn('GRANT SELECT (reservation_id) ON public.ticket_units TO "trafficxx-lab-expiry"', conn.grants)
        self.assertFalse(any('INSERT' in sql and 'reservations' in sql for sql in conn.grants))
        self.assertFalse(any('SEQUENCE' in sql or 'ALL PRIVILEGES' in sql for sql in conn.grants))

    def test_wrong_owner_aborts_before_grants(self):
        conn = connection()
        next(r for r in conn.owned if r['relname'] == 'outbox')['owner'] = 'other'
        with self.assertRaises(ValueError):
            module.grant_expiry(conn)
        self.assertFalse(conn.grants)

    def test_protected_reads_are_rejected(self):
        for table, field in [('ticket_units', 'seat_label'), ('outbox', 'payload'),
                             ('worker_heartbeats', 'name'), ('schema_migrations', 'version')]:
            with self.subTest(table=table, field=field):
                conn = connection()
                conn.column(table, field)['can_read'] = True
                with self.assertRaises(ValueError):
                    module.grant_expiry(conn)

    def test_reservation_creation_and_protected_updates_rejected(self):
        for table, field, privilege in [('reservations', 'id', 'can_insert'),
                                        ('reservations', 'expires_at', 'can_update'),
                                        ('ticket_units', 'seat_label', 'can_update'),
                                        ('outbox', 'published_at', 'can_update')]:
            with self.subTest(table=table, field=field):
                conn = connection()
                conn.column(table, field)[privilege] = True
                with self.assertRaises(ValueError):
                    module.grant_expiry(conn)

    def test_elevated_flags_and_ddl_rejected(self):
        for flag in ('admin', 'rolbypassrls', 'create_schema', 'create_objects', 'rolsuper'):
            with self.subTest(flag=flag):
                conn = connection()
                conn.access[flag] = True
                with self.assertRaises(ValueError):
                    module.grant_expiry(conn)

    def test_missing_grant_or_column_rejected(self):
        for mode in ('missing-read', 'missing-column'):
            with self.subTest(mode=mode):
                conn = connection()
                if mode == 'missing-read':
                    conn.column('ticket_units', 'reservation_id')['can_read'] = False
                else:
                    conn.columns.remove(conn.column('outbox', 'payload'))
                with self.assertRaises(ValueError):
                    module.grant_expiry(conn)

    def test_destructive_and_reference_privileges_rejected(self):
        for privilege in ('destructive', 'can_reference'):
            with self.subTest(privilege=privilege):
                conn = connection()
                conn.column('events', 'id')[privilege] = True
                with self.assertRaises(ValueError):
                    module.grant_expiry(conn)
