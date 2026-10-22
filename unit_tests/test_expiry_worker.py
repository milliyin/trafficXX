"""Offline read-only observer and idle experiment failure-path checks."""
import contextlib
import copy
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observer = load('expiry_observer', 'observe-expiry-state.py')
worker = load('expiry_worker_check', 'check-expiry-worker.py')
RID = '00000000-0000-0000-0000-000000000123'


class Connection:
    def __init__(self, phase):
        self.statements = []
        self.session = dict(identity='trafficxx-lab-migrator', database='trafficxx', ssl=True, admin=False)
        self.row = dict(id=RID, event_id='demo-seated', quantity=1,
                        request={'event_id': 'demo-seated', 'seats': ['A2']},
                        status='HELD' if phase == 'held' else 'EXPIRED', due=phase == 'expired',
                        assignments=1 if phase == 'held' else 0,
                        matching_seat=1 if phase == 'held' else 0,
                        held_messages=1, expired_messages=0 if phase == 'held' else 1)

    def execute(self, sql, params=None):
        self.statements.append(sql.strip())
        row = self.session if 'FROM pg_stat_ssl' in sql else self.row
        return SimpleNamespace(fetchone=lambda: row)


class ExpiryWorker(unittest.TestCase):
    def test_observer_only_sets_read_only_and_selects_for_both_phases(self):
        for phase in ('held', 'expired'):
            with self.subTest(phase=phase):
                conn = Connection(phase)
                result = observer.observe(conn, RID, phase)
                self.assertEqual(result['phase'], phase)
                self.assertEqual(conn.statements[0], 'SET TRANSACTION READ ONLY')
                self.assertTrue(all(sql.startswith('SELECT') for sql in conn.statements[1:]))

    def test_wrong_state_assignment_or_outbox_count_is_not_worker_proof(self):
        for field, value in [('status', 'HELD'), ('assignments', 1), ('expired_messages', 0),
                             ('held_messages', 2), ('due', False), ('matching_seat', 1)]:
            with self.subTest(field=field):
                conn = Connection('expired')
                conn.row[field] = value
                with self.assertRaises(ValueError):
                    observer.observe(conn, RID, 'expired')

    def test_observer_rejects_wrong_identity_or_admin(self):
        for field, value in [('identity', 'trafficxx-lab-expiry'), ('admin', True), ('ssl', False)]:
            with self.subTest(field=field):
                conn = Connection('held')
                conn.session[field] = value
                with self.assertRaises(ValueError):
                    observer.observe(conn, RID, 'held')
                self.assertEqual(len(conn.statements), 2)

    def test_observer_job_uses_migrator_without_worker_selector_or_probes(self):
        source = {'spec': {'template': {'spec': {
            'serviceAccountName': 'expiry', 'automountServiceAccountToken': False,
            'containers': [{'name': 'expiry', 'image': 'fixed-digest', 'command': ['worker'],
                            'env': [{'name': 'PGUSER', 'value': 'trafficxx-lab-expiry'}],
                            'readinessProbe': {'exec': {'command': ['check']}}}]}}}}
        original = copy.deepcopy(source)
        job = worker.observer_job(source, RID, 'held', 'unique-test-job')
        pod = job['spec']['template']['spec']
        self.assertEqual(source, original)
        self.assertEqual(pod['serviceAccountName'], 'migrator')
        self.assertEqual(pod['restartPolicy'], 'Never')
        self.assertNotIn('readinessProbe', pod['containers'][0])
        self.assertEqual(pod['containers'][0]['env'][0]['value'], 'trafficxx-lab-migrator')
        self.assertEqual(job['spec']['template']['metadata']['labels'],
                         {'azure.workload.identity/use': 'true'})

    def scenario(self, mode='success'):
        clock, calls, phases = [0], [], []
        state = {'metadata': {'uid': 'worker', 'generation': 1}}

        def call(index, method, path, customer, key=None, body=None):
            calls.append((method, path))
            if path.endswith('/cancel'):
                return 200, {'status': 'CANCELLED'}
            self.assertEqual(body, {'event_id': 'demo-seated', 'seats': ['A2']})
            return 201, dict(id=RID, status='HELD', seats=['A2'],
                             created_at='2026-10-23T00:00:00Z', expires_at='2026-10-23T00:05:00Z')

        def inspect_state(deployment, reservation_id, phase):
            phases.append(phase)
            if phase == mode:
                raise ValueError('Observer failed')
            return observer.observe(Connection(phase), reservation_id, phase)

        def snapshot():
            generation = 2 if mode == 'changed-worker' and clock[0] else 1
            return {'metadata': {'uid': 'worker', 'generation': generation}}

        def sleep(seconds):
            clock[0] += seconds

        return call, inspect_state, snapshot, clock, sleep, calls, phases

    def run_scenario(self, mode='success'):
        call, inspect_state, snapshot, clock, sleep, calls, phases = self.scenario(mode)
        with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(worker.time, 'sleep', side_effect=sleep), \
                contextlib.redirect_stdout(io.StringIO()):
            result = worker.experiment(call, 'customer', 'key', snapshot, inspect_state)
        return result, calls, phases, clock

    def test_success_makes_no_expiry_triggering_api_calls_after_create(self):
        result, calls, phases, clock = self.run_scenario()
        self.assertEqual(result['result'], 'EXPIRY_WORKER_OK')
        self.assertEqual(calls, [('POST', '/reservations')])
        self.assertEqual(phases, ['held', 'expired'])
        self.assertEqual(clock[0], 310)

    def test_failed_observation_attempts_cleanup_without_success(self):
        for phase in ('held', 'expired'):
            with self.subTest(phase=phase):
                call, inspect_state, snapshot, clock, sleep, calls, _ = self.scenario(phase)
                with patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(worker.time, 'sleep', side_effect=sleep), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(ValueError):
                        worker.experiment(call, 'customer', 'key', snapshot, inspect_state)
                self.assertEqual(calls[-1], ('POST', '/reservations/' + RID + '/cancel'))

    def test_deployment_change_cannot_pass_as_stable_worker_test(self):
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.run_scenario('changed-worker')
