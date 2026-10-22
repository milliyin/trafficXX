import copy
import importlib.util
from pathlib import Path
import unittest
from test_duplicate_baseline import Connection, module as baseline

spec = importlib.util.spec_from_file_location(
    'compare_duplicate', Path(__file__).resolve().parents[1] / 'scripts/compare-duplicate-state.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CompareDuplicateState(unittest.TestCase):
    def fixture(self):
        state = baseline.snapshot(Connection())
        state['message_id'] = '151af373-f74d-4bc4-8c14-e3b6cf13aa51'
        return state

    def test_exact_fingerprints_and_counts_pass(self):
        state = self.fixture()
        self.assertEqual(module.compare(state, copy.deepcopy(state))['result'], 'DUPLICATE_STATE_UNCHANGED')

    def test_each_table_count_or_fingerprint_change_fails(self):
        state = self.fixture()
        for table in state['tables']:
            for field, value in [('count', 13), ('sha256', 'f' * 64)]:
                with self.subTest(table=table, field=field):
                    other = copy.deepcopy(state)
                    other['tables'][table][field] = value
                    with self.assertRaises(ValueError):
                        module.compare(state, other)

    def test_failed_result_wrong_target_identity_or_missing_tables_fail(self):
        state = self.fixture()
        for change in ({'result': 'DUPLICATE_BASELINE_FAILED'}, {'message_id': 'different'},
                       {'outbox_id': 4}, {'read_only': False}, {'ssl': False},
                       {'observer_identity': 'other'}, {'tables': {}}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    module.compare(state, {**state, **change})

    def test_identical_invalid_hashes_cannot_pass(self):
        state = self.fixture()
        state['tables']['outbox']['sha256'] = 'invalid'
        with self.assertRaises(ValueError):
            module.compare(state, state)
