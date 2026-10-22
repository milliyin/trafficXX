"""Effective permission boundary for outbox publication and heartbeat upserts."""
import unittest
from test_booking_grants import Connection, module


READ_COLUMNS = {('worker_heartbeats', c) for c in ('name', 'last_error')}
INSERTS = {('worker_heartbeats', c) for c in ('name', 'last_error')}
UPDATES = {('outbox', c) for c in ('published_at', 'last_error', 'publish_attempts',
                                'last_attempt_at', 'publish_failures', 'next_attempt_at')}
UPDATES |= {('worker_heartbeats', c) for c in ('last_seen_at', 'last_error')}
OUTBOX_COLUMNS = ('id', 'reservation_id', 'event_type', 'payload', 'created_at',
                  'published_at', 'message_id', 'publish_attempts', 'publish_failures',
                  'next_attempt_at', 'last_attempt_at', 'last_error')


def connection():
    conn = Connection()
    existing = {(r['relname'], r['attname']) for r in conn.columns}
    extras = [('outbox', c) for c in OUTBOX_COLUMNS]
    extras += [('worker_heartbeats', c) for c in ('name', 'last_seen_at', 'last_error')]
    for pair in extras:
        if pair not in existing:
            conn.columns.append(dict(relname=pair[0], attname=pair[1],
                                     can_reference=False, destructive=False))
    for row in conn.columns:
        pair = (row['relname'], row['attname'])
        row.update(can_read=pair[0] == 'outbox' or pair in READ_COLUMNS,
                   can_insert=pair in INSERTS, can_update=pair in UPDATES)
    return conn


class PublisherGrants(unittest.TestCase):
    def test_publication_contract_excludes_reservations_and_outbox_creation(self):
        conn = connection()
        result = module.grant_publisher(conn)
        self.assertEqual(result['result'], 'PUBLISHER_GRANTS_OK')
        self.assertFalse(result['reservation_access'])
        self.assertFalse(result['inventory_access'])
        self.assertFalse(result['outbox_insert'])
        self.assertIn('GRANT SELECT (name, last_error) ON public.worker_heartbeats TO "trafficxx-lab-publisher"', conn.grants)
        self.assertFalse(any('SEQUENCE' in sql or 'ALL PRIVILEGES' in sql for sql in conn.grants))

    def test_wrong_owner_aborts_without_grants(self):
        conn = connection()
        next(r for r in conn.owned if r['relname'] == 'worker_heartbeats')['owner'] = 'other'
        with self.assertRaises(ValueError):
            module.grant_publisher(conn)
        self.assertFalse(conn.grants)

    def test_protected_reads_rejected(self):
        for table, field in [('reservations', 'customer_id'), ('events', 'id'),
                             ('ticket_units', 'reservation_id'), ('consumer_inbox', 'message_id'),
                             ('schema_migrations', 'version'), ('worker_heartbeats', 'last_seen_at')]:
            with self.subTest(table=table, field=field):
                conn = connection()
                conn.column(table, field)['can_read'] = True
                with self.assertRaises(ValueError):
                    module.grant_publisher(conn)

    def test_payload_identity_and_heartbeat_key_updates_rejected(self):
        for table, field in [('outbox', 'payload'), ('outbox', 'message_id'),
                             ('outbox', 'reservation_id'), ('worker_heartbeats', 'name')]:
            with self.subTest(table=table, field=field):
                conn = connection()
                conn.column(table, field)['can_update'] = True
                with self.assertRaises(ValueError):
                    module.grant_publisher(conn)

    def test_outbox_insert_rejected(self):
        conn = connection()
        conn.column('outbox', 'payload')['can_insert'] = True
        with self.assertRaises(ValueError):
            module.grant_publisher(conn)

    def test_missing_upsert_reads_updates_or_outbox_columns_rejected(self):
        for mode in ('conflict-key', 'excluded-error', 'heartbeat-update', 'outbox-column'):
            with self.subTest(mode=mode):
                conn = connection()
                if mode == 'conflict-key':
                    conn.column('worker_heartbeats', 'name')['can_read'] = False
                elif mode == 'excluded-error':
                    conn.column('worker_heartbeats', 'last_error')['can_read'] = False
                elif mode == 'heartbeat-update':
                    conn.column('worker_heartbeats', 'last_seen_at')['can_update'] = False
                else:
                    conn.columns.remove(conn.column('outbox', 'created_at'))
                with self.assertRaises(ValueError):
                    module.grant_publisher(conn)

    def test_admin_and_ddl_rejected(self):
        for flag in ('admin', 'create_objects', 'create_schema', 'rolsuper', 'rolbypassrls'):
            with self.subTest(flag=flag):
                conn = connection()
                conn.access[flag] = True
                with self.assertRaises(ValueError):
                    module.grant_publisher(conn)

    def test_destructive_and_reference_privileges_rejected(self):
        for privilege in ('destructive', 'can_reference'):
            with self.subTest(privilege=privilege):
                conn = connection()
                conn.column('outbox', 'id')[privilege] = True
                with self.assertRaises(ValueError):
                    module.grant_publisher(conn)
