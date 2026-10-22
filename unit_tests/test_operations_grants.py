"""Exact read-only monitoring boundary; no application payloads or writes."""
import unittest
from test_consumer_grants import connection as consumer_connection, module

READS = {('outbox', c) for c in ('created_at', 'published_at', 'message_id', 'publish_failures')}
READS |= {('consumer_inbox', 'message_id'), ('worker_failures', 'id'),
          ('observed_dead_letters', 'queue_name')}
READS |= {('worker_heartbeats', c) for c in ('name', 'last_seen_at', 'last_error')}


def connection():
    conn = consumer_connection()
    seen = {(r['relname'], r['attname']) for r in conn.columns}
    for table, field in READS:
        if (table, field) not in seen:
            conn.columns.append(dict(relname=table, attname=field, can_reference=False, destructive=False))
    for row in conn.columns:
        row.update(can_read=(row['relname'], row['attname']) in READS, can_insert=False, can_update=False)
    return conn


class OperationsGrants(unittest.TestCase):
    def test_only_column_reads_are_granted(self):
        conn = connection()
        result = module.grant_operations(conn)
        self.assertEqual(result['result'], 'OPERATIONS_GRANTS_OK')
        for flag in ('writes', 'payload_access', 'reservation_access', 'inventory_access',
                     'outbox_payload_access', 'destructive_table_access'):
            self.assertFalse(result[flag])
        self.assertEqual(len(conn.grants), 5)
        self.assertTrue(all(s.startswith('GRANT SELECT (') and
                            s.endswith('TO "trafficxx-lab-operations"') for s in conn.grants))

    def test_ownership_is_checked_before_granting(self):
        for table in {name for name, _ in READS}:
            with self.subTest(table=table):
                conn = connection()
                next(r for r in conn.owned if r['relname'] == table)['owner'] = 'other'
                with self.assertRaises(ValueError):
                    module.grant_operations(conn)
                self.assertFalse(conn.grants)

    def test_sensitive_and_unneeded_reads_are_rejected(self):
        for pair in [('outbox', 'payload'), ('reservations', 'customer_id'), ('events', 'id'),
                     ('ticket_units', 'seat_label'), ('consumer_inbox', 'envelope'),
                     ('consumer_inbox', 'processed_at'), ('reservation_updates', 'status'),
                     ('worker_failures', 'error_code'), ('observed_dead_letters', 'message_id'),
                     ('schema_migrations', 'version')]:
            with self.subTest(pair=pair):
                conn = connection()
                conn.column(*pair)['can_read'] = True
                with self.assertRaises(ValueError):
                    module.grant_operations(conn)

    def test_any_insert_update_reference_or_destructive_access_is_rejected(self):
        for flag in ('can_insert', 'can_update', 'can_reference', 'destructive'):
            for pair in READS:
                with self.subTest(flag=flag, pair=pair):
                    conn = connection()
                    conn.column(*pair)[flag] = True
                    with self.assertRaises(ValueError):
                        module.grant_operations(conn)

    def test_every_required_read_and_column_must_exist(self):
        for pair in READS:
            for missing in (False, True):
                with self.subTest(pair=pair, missing=missing):
                    conn = connection()
                    if missing:
                        conn.columns.remove(conn.column(*pair))
                    else:
                        conn.column(*pair)['can_read'] = False
                    with self.assertRaises(ValueError):
                        module.grant_operations(conn)

    def test_admin_ddl_and_missing_connect_usage_fail(self):
        for flag in connection().access:
            with self.subTest(flag=flag):
                conn = connection()
                conn.access[flag] = not conn.access[flag]
                with self.assertRaises(ValueError):
                    module.grant_operations(conn)
