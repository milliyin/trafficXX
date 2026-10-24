"""Response and parameter boundaries; SQL semantics also run on PostgreSQL in CI."""
import unittest
from unittest.mock import MagicMock

from trafficxx.browse_queries import AVAILABILITY_SQL, fetch_availability


class BrowseQueries(unittest.TestCase):
    def test_one_parameterized_statement_preserves_labels_and_metadata(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {
            'id': 'seated', 'name': 'Example', 'inventory_type': 'SEATED',
            '_available_seats': ['A2', 'A10']}
        row = fetch_availability(conn, "seated' OR true --")
        conn.execute.assert_called_once_with(AVAILABILITY_SQL, ("seated' OR true --",))
        self.assertEqual(row, {'id': 'seated', 'name': 'Example', 'inventory_type': 'SEATED',
                               'available': 2, 'seats': ['A2', 'A10']})
        self.assertNotIn('_available_seats', row)

    def test_empty_and_general_units_and_missing_event(self):
        conn = MagicMock()
        for kind, labels, seats in [('GENERAL', [None, None], None),
                                    ('GENERAL', [], None), ('SEATED', [], [])]:
            conn.execute.return_value.fetchone.return_value = {
                'id': 'test', 'inventory_type': kind, '_available_seats': labels}
            row = fetch_availability(conn, 'test')
            self.assertEqual(row['available'], len(labels))
            self.assertEqual(row['seats'], seats)
        conn.execute.return_value.fetchone.return_value = None
        self.assertIsNone(fetch_availability(conn, 'missing'))
