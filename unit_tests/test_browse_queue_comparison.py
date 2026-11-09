"""Owned temporary queue changes; no Azure or Kubernetes operations."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('queue_comparison', ROOT / 'scripts/browse-queue-comparison.py')
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
s = queue.s


def app():
    return {'metadata': {'uid': 'owned', 'resourceVersion': '17', 'annotations': {}},
            'spec': {'replicas': 2, 'strategy': {'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1}},
                     'template': {'spec': {'containers': [{'name': 'browse', 'image': 'fixed-image', 'env': [
                         {'name': 'BROWSE_DB_POOL_MIN', 'value': '8'},
                         {'name': 'BROWSE_DB_POOL_MAX', 'value': '8'},
                         {'name': 'BROWSE_DB_POOL_MAX_WAITING', 'value': '16'}]}]}}},
            'status': {'readyReplicas': 2}}


class QueueComparison(unittest.TestCase):
    def test_patch_changes_only_queue_and_marker_and_restores_exact_template(self):
        state = app()
        original = deepcopy(state['spec']['template'])
        patches = []
        def kube(*args, **kwargs):
            if args[0] == 'rollout':
                return 'rolled out'
            self.assertEqual(args[:3], ('patch', 'deployment', 'browse'))
            operations = json.loads(args[args.index('-p') + 1])
            patches.append(operations)
            self.assertEqual([o['path'] for o in operations if o['op'] == 'test'],
                             ['/metadata/uid', '/metadata/resourceVersion', '/spec/template', '/metadata/annotations'])
            replace = next(o for o in operations if o['op'] == 'replace')
            self.assertEqual(replace['path'], '/spec/template/spec/containers/0/env/2/value')
            state['spec']['template']['spec']['containers'][0]['env'][2]['value'] = replace['value']
            marker = operations[-1]
            if marker['op'] == 'add':
                state['metadata']['annotations'][queue.MARKER] = marker['value']
            else:
                del state['metadata']['annotations'][queue.MARKER]
            return '{}'
        with patch.object(s.trial, 'deployment', return_value=state), patch.object(s.trial, 'no_active_load'), \
                patch.object(s, 'owned_hpa'), patch.object(s.base, 'kube', side_effect=kube), \
                patch.object(queue, 'submit_patch', side_effect=lambda ops: kube('patch', 'deployment', 'browse', '-p', json.dumps(ops))), \
                patch.object(s, 'prometheus'), patch.object(s, 'wait_baseline'), redirect_stdout(io.StringIO()):
            queue.patch_queue(32, expected_run='a' * 12)
            self.assertEqual(queue.marker_for(state)['run_id'], 'a' * 12)
            queue.patch_queue(16, expected_run='a' * 12)
        self.assertEqual(state['spec']['template'], original)
        self.assertNotIn(queue.MARKER, state['metadata']['annotations'])
        self.assertEqual(s.EXPECTED_QUEUE.get(), 16)
        self.assertEqual(len(patches), 2)

    def test_patch_api_error_is_categorized_without_raw_response(self):
        result = SimpleNamespace(returncode=1, stderr='Forbidden: private-object-and-credential', stdout='private')
        with patch.object(queue.subprocess, 'run', return_value=result) as call:
            with self.assertRaisesRegex(ValueError, '^QUEUE_PATCH_NOT_AUTHORIZED$') as caught:
                queue.submit_patch([])
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(call.call_args.kwargs['timeout'], 30)

    def test_restore_refuses_changed_template_foreign_marker_and_active_load(self):
        state = app()
        state['spec']['template']['spec']['containers'][0]['env'][2]['value'] = '32'
        state['metadata']['annotations'][queue.MARKER] = json.dumps({
            'run_id': 'a' * 12, 'template_sha': s.trial.fingerprint(state['spec']['template']), 'deployment_uid': 'owned'})
        queue.marker_for(state, 'a' * 12)
        recreated = deepcopy(state)
        recreated['metadata']['uid'] = 'new-deployment'
        with self.assertRaisesRegex(ValueError, 'QUEUE_OWNERSHIP_CHANGED'):
            queue.marker_for(recreated)
        with self.assertRaisesRegex(ValueError, 'QUEUE_OWNERSHIP_CHANGED'):
            queue.marker_for(state, 'b' * 12)
        state['spec']['template']['spec']['containers'][0]['image'] = 'operator-image'
        with patch.object(s.trial, 'context'), patch.object(s.trial, 'no_active_load'), \
                patch.object(s.trial, 'deployment', return_value=state), patch.object(s, 'restore_prewarm') as restore:
            with self.assertRaisesRegex(ValueError, 'QUEUE_OWNERSHIP_CHANGED'):
                queue.restore()
            restore.assert_not_called()
        with patch.object(s.trial, 'context'), \
                patch.object(s.trial, 'no_active_load', side_effect=ValueError('OTHER_READ_LOAD_ACTIVE')), \
                patch.object(s.trial, 'deployment') as read:
            with self.assertRaisesRegex(ValueError, 'OTHER_READ_LOAD_ACTIVE'):
                queue.restore()
            read.assert_not_called()

    def test_run_captures_then_restores_on_success_and_failed_workload(self):
        for failure, restore_fails in ((False, False), (True, False), (True, True)):
            events = []
            primary = ValueError('LOAD_JOB_FAILED')
            def workload(**kwargs):
                self.assertEqual(kwargs, {'profile': True, 'measured': 'queue32'})
                self.assertEqual(s.EXPECTED_QUEUE.get(), 32)
                events.append('load')
                if failure:
                    raise primary
            def kube(*args):
                self.assertEqual(args[:2], ('get', 'job'))
                return '' if not events else 'job/preserved'
            def command(args, **kwargs):
                if args[0] == 'bash':
                    self.assertIn('--recent-checks', args)
                    events.append('build')
                else:
                    self.assertEqual(args[args.index('--queue-limit') + 1], '32')
                    events.append('baseline')
                return json.dumps({'result': 'BROWSE_TIMING_BASELINE_OK'})
            def restore(**kwargs):
                events.append('restore')
                if restore_fails:
                    raise ValueError('OTHER_READ_LOAD_ACTIVE')
            with ExitStack() as stack:
                for owner, name in ((s.trial, 'context'), (s.trial, 'no_active_load'), (s.trial, 'health')):
                    stack.enter_context(patch.object(owner, name))
                stack.enter_context(patch.object(s.trial, 'deployment', return_value=app()))
                stack.enter_context(patch.object(s.base, 'kube', side_effect=kube))
                stack.enter_context(patch.object(s.base, 'command', side_effect=command))
                stack.enter_context(patch.object(s, 'deploy_image', side_effect=lambda: events.append('deploy')))
                stack.enter_context(patch.object(queue, 'patch_queue', side_effect=lambda *a, **kw: events.append('queue32')))
                stack.enter_context(patch.object(s, 'drill_prewarmed', side_effect=workload))
                capture = stack.enter_context(patch.object(s, 'diagnose_500', side_effect=lambda **kw: events.append('report')))
                stack.enter_context(patch.object(queue, 'restore', side_effect=restore))
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                if failure:
                    with self.assertRaises(ValueError) as caught:
                        queue.run()
                    self.assertIs(caught.exception, primary)
                    self.assertNotIn('BROWSE_QUEUE32_COMPARISON_OK', output.getvalue())
                else:
                    queue.run()
                    self.assertIn('BROWSE_QUEUE32_COMPARISON_OK', output.getvalue())
                self.assertEqual(events, ['build', 'deploy', 'queue32', 'baseline', 'load', 'report', 'restore'])
                capture.assert_called_once_with(users=1000, distribution=True, duration=420, prewarmed=True,
                                                measure_all=True, measured='queue32')
                self.assertEqual(s.EXPECTED_QUEUE.get(), 16)

    def test_queue32_metrics_require_exact_limit_series_count_and_freshness(self):
        values = {'count': 4, 'min': 32, 'max': 32, 'timestamp': 100}
        def query(expression):
            key = 'timestamp' if 'timestamp(' in expression else expression.split('(')[0]
            return [{'value': [100, str(values[key])]}]
        with s.queue_expectation(32), patch.object(s.time, 'time', return_value=100):
            s.check_queue_metrics(query, 4)
            for key, bad in (('count', 3), ('min', 16), ('max', 33), ('timestamp', 54)):
                original = values[key]
                values[key] = bad
                with self.assertRaisesRegex(ValueError, 'QUEUE_POLICY_METRICS_MISMATCH'):
                    s.check_queue_metrics(query, 4)
                values[key] = original
        with patch.object(s, 'scalar') as scalar:
            s.check_queue_metrics(query, 4)
            scalar.assert_not_called()  # Legacy default remains strict and unchanged.

    def test_existing_job_blocks_before_build(self):
        with patch.object(s.trial, 'context'), patch.object(s.trial, 'no_active_load'), \
                patch.object(s.base, 'kube', return_value='job/exists'), patch.object(s.base, 'command') as build:
            with self.assertRaisesRegex(ValueError, 'INSPECT_EXISTING_DRILL_JOB'):
                queue.run()
            build.assert_not_called()

    def test_queue32_expectation_is_scoped_and_actual_policy_guarded(self):
        state = app()
        state['spec']['template']['spec']['containers'][0]['env'][2]['value'] = '32'
        with patch.object(s.trial, 'deployment', return_value=state):
            with self.assertRaisesRegex(ValueError, 'POOL_POLICY_CHANGED'):
                s.deployment()
            with s.queue_expectation(32):
                s.deployment()
                with self.assertRaises(ValueError):
                    with s.queue_expectation(64):
                        self.fail('unreviewed policy')
        self.assertEqual(s.EXPECTED_QUEUE.get(), 16)


if __name__ == '__main__':
    unittest.main()
