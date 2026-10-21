"""Offline non-destructive demo seed decisions; no database connection."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('demo_seed', ROOT / 'scripts/bootstrap-azure-schema.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Connection:
    def __init__(self):
        self.events = {}
        self.units = {}
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith('SELECT pg_advisory'):
            return None
        if sql.startswith('SELECT id, name'):
            return SimpleNamespace(fetchone=lambda: self.events.get(params[0]))
        if sql.startswith('SELECT unit_number'):
            return SimpleNamespace(fetchall=lambda: [
                {'unit_number': row['unit_number'], 'seat_label': row['seat_label']}
                for row in self.units.get(params[0], [])[:101]])
        if sql.startswith('INSERT INTO public.events'):
            event_id, name, inventory_type = params
            self.events[event_id] = {'id': event_id, 'name': name, 'inventory_type': inventory_type}
        elif sql.startswith('INSERT INTO public.ticket_units'):
            event_id, inventory_type = params
            self.units[event_id] = [
                {'unit_number': n, 'seat_label': f'A{n}' if inventory_type == 'SEATED' else None,
                 'reservation_id': None} for n in range(1, 101)]
        else:
            raise AssertionError('Unexpected SQL')
        return None


class DemoSeed(unittest.TestCase):
    def seeded(self):
        connection = Connection()
        module.seed_demo(connection)
        connection.statements.clear()
        return connection

    def test_fresh_seed_adds_two_events_and_one_hundred_units_each(self):
        connection = Connection()
        result = module.seed_demo(connection)
        self.assertEqual(result['created'], ['demo-general', 'demo-seated'])
        self.assertEqual(result['skipped'], [])
        self.assertEqual(len(connection.units['demo-general']), 100)
        self.assertEqual(connection.units['demo-seated'][-1]['seat_label'], 'A100')

    def test_repeat_skips_existing_inventory_without_writes(self):
        connection = self.seeded()
        result = module.seed_demo(connection)
        self.assertEqual(result['created'], [])
        self.assertEqual(result['skipped'], ['demo-general', 'demo-seated'])
        self.assertTrue(all(sql.startswith('SELECT') for sql in connection.statements))

    def test_assigned_reservation_is_preserved_on_retry(self):
        connection = self.seeded()
        connection.units['demo-seated'][0]['reservation_id'] = 'existing-booking'
        module.seed_demo(connection)
        self.assertEqual(connection.units['demo-seated'][0]['reservation_id'], 'existing-booking')

    def test_changed_event_definition_is_rejected(self):
        connection = self.seeded()
        connection.events['demo-general']['inventory_type'] = 'SEATED'
        with self.assertRaisesRegex(ValueError, 'definition'):
            module.seed_demo(connection)
        self.assertTrue(all(sql.startswith('SELECT') for sql in connection.statements))

    def test_missing_unit_is_not_silently_replenished(self):
        connection = self.seeded()
        connection.units['demo-general'].pop()
        with self.assertRaisesRegex(ValueError, 'refusing to repair or reset'):
            module.seed_demo(connection)
        self.assertEqual(len(connection.units['demo-general']), 99)

    def test_extra_unit_is_rejected(self):
        connection = self.seeded()
        connection.units['demo-general'].append({'unit_number': 101, 'seat_label': None})
        with self.assertRaises(ValueError):
            module.seed_demo(connection)

    def test_changed_seat_label_is_rejected(self):
        connection = self.seeded()
        connection.units['demo-seated'][0]['seat_label'] = 'OTHER'
        with self.assertRaises(ValueError):
            module.seed_demo(connection)

    def test_unrelated_event_is_untouched(self):
        connection = self.seeded()
        connection.events['unrelated'] = {'name': 'Leave this alone'}
        module.seed_demo(connection)
        self.assertEqual(connection.events['unrelated'], {'name': 'Leave this alone'})


if __name__ == '__main__':
    unittest.main()
