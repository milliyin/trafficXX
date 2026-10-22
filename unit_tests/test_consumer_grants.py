"""Consumer effective permissions, including protected data and generated fields."""
import unittest
from test_booking_grants import Connection, module


FIELDS = {
    'consumer_inbox': ('message_id', 'envelope', 'processed_at'),
    'reservation_updates': ('reservation_id', 'event_id', 'status', 'last_outbox_id', 'updated_at'),
    'worker_heartbeats': ('name', 'last_seen_at', 'last_error'),
    'worker_failures': ('id', 'message_id', 'error_code', 'occurred_at'),
    'observed_dead_letters': ('queue_name', 'sequence_number', 'message_id', 'reason', 'observed_at'),
}
READS = {
    ('consumer_inbox', 'message_id'), ('consumer_inbox', 'envelope'),
    ('reservation_updates', 'reservation_id'), ('reservation_updates', 'event_id'),
    ('reservation_updates', 'status'), ('reservation_updates', 'last_outbox_id'),
    ('worker_heartbeats', 'name'), ('worker_heartbeats', 'last_error'),
}
INSERTS = READS | {
    ('worker_failures', 'message_id'), ('worker_failures', 'error_code'),
    ('observed_dead_letters', 'queue_name'), ('observed_dead_letters', 'sequence_number'),
    ('observed_dead_letters', 'message_id'), ('observed_dead_letters', 'reason'),
}
UPDATES = {
    ('reservation_updates', 'event_id'), ('reservation_updates', 'status'),
    ('reservation_updates', 'last_outbox_id'), ('reservation_updates', 'updated_at'),
    ('worker_heartbeats', 'last_seen_at'), ('worker_heartbeats', 'last_error'),
}


def connection():
    conn = Connection()
    existing = {(r['relname'], r['attname']) for r in conn.columns}
    for table, fields in FIELDS.items():
        for field in fields:
            if (table, field) not in existing:
                conn.columns.append(dict(relname=table, attname=field,
                                         can_reference=False, destructive=False))
    for row in conn.columns:
        pair = (row['relname'], row['attname'])
        row.update(can_read=pair in READS, can_insert=pair in INSERTS, can_update=pair in UPDATES)
    return conn


class ConsumerGrants(unittest.TestCase):
    def test_contract_excludes_booking_inventory_and_outbox(self):
        conn = connection()
        result = module.grant_consumer(conn)
        self.assertEqual(result['result'], 'CONSUMER_GRANTS_OK')
        for key in ('reservation_access', 'inventory_access', 'outbox_access', 'destructive_table_access'):
            self.assertFalse(result[key])
        self.assertEqual(set(result['insert_tables']), set(FIELDS))
        self.assertEqual(len(conn.grants), 10)
        self.assertTrue(all('TO "trafficxx-lab-consumer"' in sql for sql in conn.grants))
        self.assertFalse(any('SEQUENCE' in sql or 'ALL PRIVILEGES' in sql or
                             'GRANT SELECT ON' in sql for sql in conn.grants))

    def test_all_five_tables_require_migrator_ownership_before_grants(self):
        for table in FIELDS:
            with self.subTest(table=table):
                conn = connection()
                next(r for r in conn.owned if r['relname'] == table)['owner'] = 'other'
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)
                self.assertFalse(conn.grants)

    def test_protected_reads_rejected(self):
        for pair in [('reservations', 'customer_id'), ('events', 'id'), ('ticket_units', 'seat_label'),
                     ('outbox', 'payload'), ('schema_migrations', 'version'),
                     ('consumer_inbox', 'processed_at'), ('reservation_updates', 'updated_at'),
                     ('worker_heartbeats', 'last_seen_at'), ('worker_failures', 'error_code'),
                     ('observed_dead_letters', 'reason')]:
            with self.subTest(pair=pair):
                conn = connection()
                conn.column(*pair)['can_read'] = True
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)

    def test_immutable_inbox_projection_key_and_failure_updates_rejected(self):
        for pair in [('consumer_inbox', 'envelope'), ('reservation_updates', 'reservation_id'),
                     ('worker_heartbeats', 'name'), ('worker_failures', 'error_code'),
                     ('observed_dead_letters', 'reason'), ('reservations', 'status')]:
            with self.subTest(pair=pair):
                conn = connection()
                conn.column(*pair)['can_update'] = True
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)

    def test_generated_metadata_and_base_table_inserts_rejected(self):
        for pair in [('worker_failures', 'id'), ('worker_failures', 'occurred_at'),
                     ('consumer_inbox', 'processed_at'), ('reservation_updates', 'updated_at'),
                     ('worker_heartbeats', 'last_seen_at'), ('observed_dead_letters', 'observed_at'),
                     ('outbox', 'payload'), ('reservations', 'id')]:
            with self.subTest(pair=pair):
                conn = connection()
                conn.column(*pair)['can_insert'] = True
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)

    def test_every_required_read_insert_update_and_column_must_exist(self):
        for privilege, pairs in [('can_read', READS), ('can_insert', INSERTS), ('can_update', UPDATES)]:
            for pair in pairs:
                with self.subTest(privilege=privilege, pair=pair):
                    conn = connection()
                    conn.column(*pair)[privilege] = False
                    with self.assertRaises(ValueError):
                        module.grant_consumer(conn)
        for pair in READS | INSERTS | UPDATES:
            with self.subTest(missing=pair):
                conn = connection()
                conn.columns.remove(conn.column(*pair))
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)

    def test_admin_ddl_and_missing_connect_usage_rejected(self):
        for flag in connection().access:
            with self.subTest(flag=flag):
                conn = connection()
                conn.access[flag] = not conn.access[flag]
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)

    def test_destructive_and_reference_access_rejected(self):
        for privilege in ('destructive', 'can_reference'):
            with self.subTest(privilege=privilege):
                conn = connection()
                conn.column('consumer_inbox', 'message_id')[privilege] = True
                with self.assertRaises(ValueError):
                    module.grant_consumer(conn)
