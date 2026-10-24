"""Bounded write intent cleanup, ambiguity handling and final evidence gates."""
import contextlib
import importlib.util
import io
import itertools
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('write_baseline', ROOT / 'scripts/check-booking-write-baseline.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class API:
    def __init__(self):
        self.keys, self.rows, self.calls = {}, {}, []
        self.lock = threading.Lock()

    def __call__(self, index, method, path, customer, key=None, body=None):
        with self.lock:
            self.calls.append((index, method, path, customer, key, body))
            if path == '/reservations':
                self.assert_body(body)
                identity = customer, key
                if identity in self.keys:
                    return 200, dict(self.rows[self.keys[identity]])
                identifier = str(uuid4())
                self.keys[identity] = identifier
                self.rows[identifier] = {'id': identifier, 'status': 'HELD', **body}
                return 201, dict(self.rows[identifier])
            identifier = path.split('/')[2]
            if path.endswith('/cancel'):
                self.rows[identifier]['status'] = 'CANCELLED'
            return 200, dict(self.rows[identifier])

    @staticmethod
    def assert_body(body):
        if body != {'event_id': 'demo-general', 'quantity': 1}:
            raise AssertionError('Unexpected inventory scope')


class BookingWriteBaseline(unittest.TestCase):
    def journal(self, folder):
        return module.Journal(Path(folder) / 'journal.json', 'lab-test', 'original-key')

    def test_all_eighteen_intents_are_unique_cancelled_and_read_from_both_replicas(self):
        api = API()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(module.time, 'monotonic', side_effect=itertools.count()), \
                contextlib.redirect_stdout(io.StringIO()):
            path = Path(folder) / 'journal.json'
            result = module.experiment(api, 'lab-test', 'key', path)
            saved = json.loads(path.read_text())
        self.assertEqual(result['created_and_cancelled'], 18)
        self.assertEqual([row['completed_pairs'] for row in result['phases']], [6, 12])
        self.assertEqual(len(api.calls), 90)  # Create, timed cancel, cleanup cancel, two reads.
        self.assertEqual(len(api.keys), 18)
        self.assertTrue(all(row['status'] == 'CANCELLED' for row in api.rows.values()))
        self.assertTrue(all(row['attempted'] and row['cleanup_verified'] for row in saved['intents']))
        self.assertEqual(len({row['id'] for row in saved['intents']}), 18)
        self.assertFalse(result['latency_slo_tested'])
        for row in saved['intents']:
            reads = {call[0] for call in api.calls if call[1] == 'GET' and row['id'] in call[2]}
            self.assertEqual(reads, {0, 1})

    def test_intent_is_persisted_before_create(self):
        api = API()
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)

            def call(*args):
                if args[2] == '/reservations':
                    saved = json.loads(journal.path.read_text())['intents'][0]
                    self.assertTrue(saved['attempted'])
                    self.assertEqual(saved['key'], args[4])
                return api(*args)
            module.flow(call, journal, journal.rows[0])

    def test_lost_create_response_uses_same_key_and_cleans_up_but_does_not_pass(self):
        api, failed = API(), False

        def call(*args):
            nonlocal failed
            response = api(*args)
            if args[2] == '/reservations' and not failed:
                failed = True
                raise OSError('PRIVATE')
            return response
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            with self.assertRaises(OSError):
                module.flow(call, journal, journal.rows[0])
            self.assertTrue(journal.rows[0]['cleanup_verified'])
        creates = [row for row in api.calls if row[2] == '/reservations']
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0][3:], creates[1][3:])
        self.assertEqual(len(api.keys), 1)

    def test_lost_cancel_response_is_cleaned_without_hiding_workload_failure(self):
        api, failed = API(), False

        def call(*args):
            nonlocal failed
            response = api(*args)
            if args[2].endswith('/cancel') and not failed:
                failed = True
                raise OSError('PRIVATE')
            return response
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            with self.assertRaises(OSError):
                module.flow(call, journal, journal.rows[0])
            self.assertTrue(journal.rows[0]['cleanup_verified'])

    def test_cleanup_failure_retains_id_and_unresolved_flag(self):
        api = API()

        def call(*args):
            if args[2].endswith('/cancel'):
                return 500, {'detail': 'PRIVATE'}
            return api(*args)
        with tempfile.TemporaryDirectory() as folder:
            journal = self.journal(folder)
            with self.assertRaises(module.CheckFailed):
                module.flow(call, journal, journal.rows[0])
            saved = json.loads(journal.path.read_text())['intents'][0]
            UUID(saved['id'])
            self.assertTrue(saved['attempted'])
            self.assertFalse(saved['cleanup_verified'])

    def test_partial_failure_stops_before_second_phase_and_retains_cleanup(self):
        api = API()

        def call(*args):
            response = api(*args)
            if args[2] == '/reservations':
                return 500, response[1]
            return response
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            path = Path(folder) / 'journal.json'
            with self.assertRaises(module.CheckFailed):
                module.experiment(call, 'lab-test', 'key', path)
            rows = json.loads(path.read_text())['intents']
        self.assertEqual(sum(row['attempted'] for row in rows), 1)
        self.assertTrue(rows[0]['cleanup_verified'])
        self.assertEqual(len(api.keys), 1)

    def test_journal_refuses_overwriting_previous_run(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.journal(folder)
            original = first.path.read_bytes()
            with self.assertRaises(FileExistsError):
                self.journal(folder)
            self.assertEqual(first.path.read_bytes(), original)

    def test_wrong_subscription_prevents_cluster_access(self):
        with patch.object(module.subprocess, 'check_output', return_value='wrong'), \
                patch.object(module, 'load') as load, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(), 1)
        load.assert_not_called()
        self.assertIn('WRONG_SUBSCRIPTION', output.getvalue())

    def test_partial_inventory_does_not_pass_demo_gate(self):
        payload = {'result': 'BROWSE_DEMO_HTTP_OK', 'demo_availability': {'demo-general': 99, 'demo-seated': 100}}
        with patch.object(module.subprocess, 'check_output', return_value=json.dumps(payload)), \
                self.assertRaises(module.CheckFailed):
            module.availability()

    def test_workload_success_needs_post_inventory_and_unchanged_pods(self):
        for changed in (False, True):
            snapshots = iter([{'pods': [{'image': 'digest', 'uid': 'before'}]}] * 2
                             + [{'pods': [{'image': 'digest', 'uid': 'changed' if changed else 'before'}]}] * 2)
            runner = SimpleNamespace(kube=lambda *args: 'trafficxx-lab-aks',
                                     snapshot=lambda *args: next(snapshots))

            def base_main(**options):
                options['experiment_fn'](Mock(), 'lab-test', 'key')
                return 0
            base = SimpleNamespace(main=base_main)

            def load(name):
                return {'run-read-load': runner, 'check-browse-concurrency': object(),
                        'check-booking-concurrency': base}[name]
            availability = Mock(return_value={'demo-general': 100, 'demo-seated': 100})
            with patch.object(module.subprocess, 'check_output', return_value='35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'), \
                    patch.object(module, 'load', side_effect=load), \
                    patch.object(module, 'experiment', return_value={'result': 'BOOKING_WRITE_WORKLOAD_OK'}), \
                    patch.object(module, 'availability', availability), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                result = module.main()
            self.assertEqual(result, 1 if changed else 0)
            self.assertEqual(availability.call_count, 2)
            self.assertEqual('BOOKING_WRITE_BASELINE_OK' in output.getvalue(), not changed)
