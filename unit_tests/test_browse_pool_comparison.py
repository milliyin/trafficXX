"""Temporary policy trial, rollback ownership and journal recovery boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pool_trial', ROOT / 'scripts/run-browse-pool-comparison.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def app():
    return {'metadata': {'name': 'browse', 'namespace': 'trafficxx', 'uid': 'owned', 'resourceVersion': '1'},
            'spec': {'replicas': 2, 'template': {'spec': {'containers': [{'name': 'browse',
                'image': 'trafficxxacre8708a85.azurecr.io/trafficxx@sha256:' + 'a' * 64,
                'resources': {'limits': {'cpu': '500m', 'memory': '256Mi'}},
                'env': [{'name': 'BROWSE_DB_POOL_MIN', 'value': '4'},
                        {'name': 'BROWSE_DB_POOL_MAX', 'value': '4'},
                        {'name': 'BROWSE_DB_POOL_MAX_WAITING', 'value': '16'}]}]}}}}


class PoolComparison(unittest.TestCase):
    def test_journal_is_nonsecret_and_only_exact_policy_transitions_are_allowed(self):
        original = app()
        state = trial.state_for(original)
        self.assertNotIn('env', state)
        self.assertNotIn('password', json.dumps(state))
        with patch.object(trial.base, 'kube') as kube:
            trial.patch_pool(original, state, 4, 8)
        changes = json.loads(kube.call_args.args[-1])
        self.assertEqual(changes[0], {'op': 'test', 'path': '/metadata/uid', 'value': 'owned'})
        self.assertEqual(changes[1]['path'], '/metadata/resourceVersion')
        replacements = [row for row in changes if row['op'] == 'replace']
        self.assertEqual([row['path'] for row in replacements],
                         ['/spec/template/spec/containers/0/env/0/value',
                          '/spec/template/spec/containers/0/env/1/value'])
        self.assertEqual([row['value'] for row in replacements], ['8', '8'])
        for changed in (dict(original, metadata=dict(original['metadata'], uid='foreign')),
                        dict(original, spec=dict(original['spec'], replicas=3))):
            with self.assertRaises(ValueError), patch.object(trial.base, 'kube') as kube:
                trial.patch_pool(changed, state, 4, 8)
            kube.assert_not_called()
        with self.assertRaises(ValueError):
            trial.patch_pool(original, state, 4, 16)

    def scenario(self, fault=None, preflight=False, restore_only=False, duration_seconds=60):
        live, commands, health_calls = app(), [], []
        active = [fault == 'overlap']
        timestamp = datetime.now(timezone.utc).isoformat()
        def kube(*args, **kwargs):
            commands.append(args)
            if args == ('config', 'current-context'):
                return 'trafficxx-lab-aks'
            if args[:2] == ('get', 'deployment'):
                return json.dumps(live)
            if args[:2] == ('get', 'hpa'):
                return '{"items":[]}'
            if args[:2] == ('get', 'jobs'):
                return json.dumps({'items': [{'metadata': {'name': trial.JOB}, 'status': {}}] if active[0] else []})
            if args[:2] == ('get', 'job'):
                return ''
            if args[:2] == ('patch', 'deployment'):
                changes = json.loads(args[-1])
                for row in changes:
                    if row['op'] == 'replace':
                        index = int(row['path'].split('/')[7])
                        live['spec']['template']['spec']['containers'][0]['env'][index]['value'] = row['value']
                live['metadata']['resourceVersion'] = str(int(live['metadata']['resourceVersion']) + 1)
                if fault == 'lost-patch-response' and live['metadata']['resourceVersion'] == '2':
                    raise subprocess.TimeoutExpired('kubectl', 30)
                return '{}'
            self.fail('Unexpected command ' + str(args[:2]))
        def health():
            health_calls.append(True)
            if fault == 'trial-health' and len(health_calls) == 2:
                raise ValueError('HEALTH_FAILED')
        def workload(**kwargs):
            self.assertEqual(kwargs, {'users': 250, 'single_query_comparison': True, 'pool_size': 8,
                                      'duration_seconds': duration_seconds})
            trial.policy(live, 8)
            if fault == 'interrupt':
                raise KeyboardInterrupt()
            if fault == 'concurrent-template':
                live['spec']['template']['metadata'] = {'annotations': {'external': 'preserve'}}
            if fault == 'active-after-load':
                active[0] = True
            return 1 if fault == 'load-failed' else 0
        def snapshot(flow, api):
            return {'generation': 1, 'pods': [{'pod': api + '-a'}, {'pod': api + '-b'}]}
        metrics = {'items': [{'metadata': {'name': 'browse-' + suffix}, 'timestamp': timestamp,
                             'containers': [{'name': 'browse', 'usage': {'cpu': '10m', 'memory': '60Mi'}}]}
                            for suffix in ('a', 'b')]}
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            if restore_only:
                trial.save(journal, trial.state_for(live, duration_seconds), create=True)
                if fault == 'resume-eight':
                    live['spec']['template']['spec']['containers'][0]['env'][0]['value'] = '8'
                    live['spec']['template']['spec']['containers'][0]['env'][1]['value'] = '8'
            output = io.StringIO()
            with patch.object(trial.base, 'kube', side_effect=kube), \
                    patch.object(trial.base, 'command', return_value='35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'), \
                    patch.object(trial.base, 'snapshot', side_effect=snapshot), \
                    patch.object(trial.users, 'metrics', return_value=metrics), \
                    patch.object(trial, 'health', side_effect=health), \
                    patch.object(trial, 'verify_pool', side_effect=lambda size: trial.policy(live, size)), \
                    patch.object(trial, 'rollout'), patch.object(trial, 'wait_metrics'), \
                    patch.object(trial.users, 'execute', side_effect=workload) as load, \
                    patch.object(trial.time, 'monotonic', side_effect=range(0, 10000, 100)), \
                    redirect_stdout(output):
                code = trial.execute(journal=journal, preflight_only=preflight, restore_only=restore_only,
                                     duration_seconds=duration_seconds)
            saved = json.loads(journal.read_text()) if journal.exists() else None
            return code, output.getvalue(), live, saved, commands, load.call_count

    def test_success_restores_four_and_preflight_never_mutates_or_saves(self):
        code, output, live, saved, commands, calls = self.scenario()
        self.assertEqual(code, 0, output)
        self.assertIn('BROWSE_POOL8_COMPARISON_OK', output)
        self.assertIn('BROWSE_POOL4_RESTORED', output)
        trial.policy(live, 4)
        self.assertEqual(saved['state'], 'restored')
        self.assertTrue(saved['load_passed'])
        self.assertEqual(calls, 1)
        self.assertEqual(sum(args[:2] == ('patch', 'deployment') for args in commands), 2)
        code, output, live, saved, commands, calls = self.scenario(preflight=True)
        self.assertEqual(code, 0, output)
        self.assertIn('BROWSE_POOL8_PREFLIGHT_OK', output)
        self.assertIsNone(saved)
        self.assertEqual(calls, 0)
        self.assertFalse(any(args[0] == 'patch' for args in commands))

    def test_failure_interruption_and_lost_response_restore_without_hiding_failure(self):
        for fault in ('load-failed', 'interrupt', 'trial-health', 'lost-patch-response'):
            with self.subTest(fault=fault):
                code, output, live, saved, commands, calls = self.scenario(fault)
                self.assertEqual(code, 1, output)
                trial.policy(live, 4)
                self.assertEqual(saved['state'], 'restored')
                self.assertIn('BROWSE_POOL4_RESTORED', output)
                self.assertNotIn('BROWSE_POOL8_COMPARISON_OK', output)

    def test_concurrent_change_and_active_load_block_rollback_without_overwrite(self):
        for fault in ('concurrent-template', 'active-after-load'):
            code, output, live, saved, commands, calls = self.scenario(fault)
            self.assertEqual(code, 1, output)
            self.assertIn('BROWSE_POOL4_RESTORE_FAILED', output)
            self.assertNotEqual(saved['state'], 'restored')
            trial.policy(live, 8)
            if fault == 'concurrent-template':
                self.assertEqual(live['spec']['template']['metadata']['annotations']['external'], 'preserve')
            self.assertEqual(sum(args[:2] == ('patch', 'deployment') for args in commands), 1)
        code, output, live, saved, commands, calls = self.scenario('overlap')
        self.assertEqual(code, 1)
        self.assertIsNone(saved)
        self.assertEqual(calls, 0)

    def test_restore_resumes_prepared_journal_without_rerunning_workload(self):
        code, output, live, saved, commands, calls = self.scenario(restore_only=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(saved['state'], 'restored')
        self.assertEqual(calls, 0)
        self.assertFalse(any(args[0] == 'patch' for args in commands))
        code, output, live, saved, commands, calls = self.scenario('resume-eight', restore_only=True)
        self.assertEqual(code, 0, output)
        trial.policy(live, 4)
        self.assertEqual(saved['state'], 'restored')
        self.assertEqual(calls, 0)
        self.assertEqual(sum(args[:2] == ('patch', 'deployment') for args in commands), 1)

    def test_longer_trial_has_distinct_identity_and_resumes_only_matching_journal(self):
        self.assertNotEqual(trial.job_for(180), trial.job_for(60))
        state = trial.state_for(app(), 180)
        self.assertEqual(state['job'], 'browse-users-250-pool8-180s')
        with self.assertRaises(ValueError):
            trial.validate_state(state, 60)
        legacy = trial.state_for(app())
        legacy.pop('duration_seconds')
        trial.validate_state(legacy, 60)
        for preflight in (True, False):
            code, output, live, saved, commands, calls = self.scenario(preflight=preflight, duration_seconds=180)
            self.assertEqual(code, 0, output)
            self.assertIn('"duration_seconds": 180', output)
            trial.policy(live, 4)
            if not preflight:
                self.assertEqual(saved['job'], 'browse-users-250-pool8-180s')
        code, output, live, saved, commands, calls = self.scenario('resume-eight', restore_only=True, duration_seconds=180)
        self.assertEqual(code, 0, output)
        self.assertEqual(saved['state'], 'restored')
        trial.policy(live, 4)
