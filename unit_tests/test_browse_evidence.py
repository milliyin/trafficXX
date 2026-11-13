"""Retained load evidence: bounded writes, exact ownership and scoped RBAC."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e = load('evidence_tests', 'scripts/browse_evidence.py')
s = load('evidence_scaling_tests', 'scripts/browse-autoscaling.py')
q = load('evidence_queue_tests', 'scripts/browse-queue-comparison.py')
RUN = 'a' * 12
JOB = {'metadata': {'name': 'browse-users-1000-queue32-retained', 'uid': 'owned',
                    'labels': {'trafficxx.io/run-id': RUN}}}


class Evidence(unittest.TestCase):
    def test_only_own_configmap_patch_is_granted_and_records_have_no_job_ttl(self):
        base = SimpleNamespace(kube=Mock())
        resource = e.prepare(base, RUN, JOB['metadata']['name'])
        args, kwargs = base.kube.call_args
        self.assertEqual(args, ('create', '-f', '-'))
        items = json.loads(kwargs['data'])['items']
        role = next(item for item in items if item['kind'] == 'Role')
        self.assertEqual(role['rules'], [{'apiGroups': [''], 'resources': ['configmaps'],
                         'resourceNames': [resource], 'verbs': ['patch']}])
        self.assertTrue(all('ownerReferences' not in item['metadata'] for item in items))
        self.assertEqual(next(item for item in items if item['kind'] == 'RoleBinding')['subjects'],
                         [{'kind': 'ServiceAccount', 'name': resource, 'namespace': 'trafficxx'}])

    def test_retained_read_rejects_recreated_job_and_foreign_run(self):
        identity = {'run_id': RUN, 'job': JOB['metadata']['name'], 'job_uid': 'owned'}
        cm = {'metadata': {'labels': {'trafficxx.io/run-id': RUN}},
              'data': {'identity.json': json.dumps(identity), 'summary.json': '{"attempts":123}'}}
        base = SimpleNamespace(kube=Mock(side_effect=lambda *a: json.dumps(cm)))
        self.assertEqual(e.read(base, JOB, 'summary.json'), {'attempts': 123})
        identity['job_uid'] = 'other'
        cm['data']['identity.json'] = json.dumps(identity)
        with self.assertRaisesRegex(ValueError, 'IDENTITY_MISMATCH'):
            e.read(base, JOB, 'summary.json')
        identity['job_uid'] = 'owned'
        cm['data']['identity.json'] = json.dumps(identity)
        cm['metadata']['labels']['trafficxx.io/run-id'] = 'b' * 12
        with self.assertRaisesRegex(ValueError, 'IDENTITY_MISMATCH'):
            e.read(base, JOB, 'summary.json')

    def test_serialization_and_run_names_are_bounded(self):
        for value in ('../x', 'a' * 13, None):
            with self.assertRaises(ValueError):
                e.name(value)
        with self.assertRaisesRegex(ValueError, 'TOO_LARGE'):
            e.encoded({'value': 'x' * 100001})
        with self.assertRaises(ValueError):
            e.encoded({'value': float('nan')})

    def test_publish_uses_verified_tls_patch_and_never_prints_api_error(self):
        env = {'BROWSE_EVIDENCE_CONFIGMAP': e.name(RUN), 'BROWSE_EVIDENCE_RUN': RUN}
        with patch.dict(e.os.environ, env), patch.object(e.Path, 'read_text', return_value='fake-token'), \
             patch.object(e.ssl, 'create_default_context', return_value='verified') as tls, \
             patch.object(e.urllib.request, 'urlopen') as http:
            e.publish('progress.json', {'run_id': RUN, 'attempts': 12})
            req = http.call_args.args[0]
            self.assertEqual(req.get_method(), 'PATCH')
            self.assertTrue(req.full_url.endswith('/configmaps/' + e.name(RUN)))
            self.assertEqual(http.call_args.kwargs, {'context': 'verified', 'timeout': 3})
            self.assertTrue(tls.call_args.kwargs['cafile'].endswith('ca.crt'))
            http.side_effect = RuntimeError('raw-secret-error')
            with self.assertRaisesRegex(RuntimeError, '^EVIDENCE_WRITE_FAILED$'):
                e.publish('summary.json', {'run_id': RUN})
            with self.assertRaisesRegex(ValueError, 'INVALID_EVIDENCE_KEY'):
                e.publish('identity.json', {'run_id': RUN})

    def test_completed_summary_can_be_read_without_pod_or_logs(self):
        summary = {'result': 'BROWSE_SCALING_LOAD_OK', 'run_id': RUN, 'users': 1000}
        with patch.object(s, 'DURABLE_EVIDENCE', True), \
             patch.object(s.evidence, 'read', return_value=summary), patch.object(s.base, 'kube') as kube:
            self.assertEqual(json.loads(s.workload_logs(JOB)), summary)
            kube.assert_not_called()
        with patch.object(s, 'DURABLE_EVIDENCE', True), \
             patch.object(s.evidence, 'read', side_effect=ValueError('EVIDENCE_IDENTITY_MISMATCH')), \
             patch.object(s.base, 'kube') as kube:
            with self.assertRaisesRegex(ValueError, 'IDENTITY_MISMATCH'):
                s.workload_logs(JOB)
            kube.assert_not_called()

    def test_retained_cli_selects_new_job_without_reusing_old_trial(self):
        with patch.object(q, 'run') as run, patch.object(q.s, 'DURABLE_EVIDENCE', False), \
             patch.object(q.s, 'QUEUE32_PROFILE_JOB', 'browse-users-1000-queue32-profile'), \
             patch.object(q.argparse.ArgumentParser, 'parse_args',
                          return_value=SimpleNamespace(action='run-retained')):
            self.assertEqual(q.main(), 0)
            run.assert_called_once()
            self.assertTrue(q.s.DURABLE_EVIDENCE)
            self.assertEqual(q.s.QUEUE32_PROFILE_JOB, 'browse-users-1000-queue32-retained')

    def test_retained_manifest_only_changes_generator_identity_and_evidence_env(self):
        app = {'spec': {'template': {'spec': {'containers': [{'image': 'immutable'}]}}}}
        manifest = {'spec': {'template': {'spec': {'containers': [{'name': 'check',
                     'resources': {'limits': {'cpu': '1', 'memory': '256Mi'}}}]}}}}
        created = []
        def kube(*args, **kwargs):
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [{'metadata': {}, 'spec': {'nodeName': 'existing'}}]})
            if args[:2] == ('patch', '--local'):
                return json.dumps(manifest)
            if args[:2] == ('create', '-f') and kwargs.get('data') == json.dumps(manifest):
                self.fail('Unmodified manifest was submitted')
            if args[:2] == ('create', '-f'):
                try:
                    row = json.loads(kwargs.get('data', '{}'))
                except json.JSONDecodeError:
                    return '{}'
                if 'spec' in row:
                    created.append(row)
                    return json.dumps(JOB)
            return '{}'
        with patch.object(s, 'DURABLE_EVIDENCE', True), patch.object(s.base, 'kube', side_effect=kube), \
             patch.object(s.evidence, 'prepare', return_value=e.name(RUN)), patch.object(s.evidence, 'bind') as bind:
            s.create_job(app, RUN, JOB['metadata']['name'], users=1000, duration=420)
        pod = created[0]['spec']['template']['spec']
        self.assertEqual(pod['serviceAccountName'], e.name(RUN))
        self.assertTrue(pod['automountServiceAccountToken'])
        self.assertEqual(pod['containers'][0]['resources']['limits'], {'cpu': '1', 'memory': '256Mi'})
        bind.assert_called_once_with(s.base, JOB)

    def test_incomplete_exception_record_cannot_become_workload_pass(self):
        job = json.loads(json.dumps(JOB))
        job['spec'] = {'template': {'spec': {'containers': [{'name': 'check',
                        'args': [RUN, '1000', '120', '420', 'ramp-down']}]}}}
        job['status'] = {'conditions': [{'type': 'Failed', 'status': 'True',
                                       'reason': 'BackoffLimitExceeded'}]}
        failure = {'result': 'BROWSE_SCALING_LOAD_FAILED', 'run_id': RUN,
                   'error_type': 'MemoryError', 'final_workload_summary_available': False}
        with patch.object(s, 'DURABLE_EVIDENCE', True), \
             patch.object(s, 'QUEUE32_PROFILE_JOB', JOB['metadata']['name']):
            outcome = s.diagnostic_outcome(job, json.dumps(failure), allow_interrupted=True,
                                           users=1000, duration=420)
        self.assertFalse(outcome['final_workload_summary_available'])
        self.assertNotIn('attempts', outcome)
        self.assertNotIn('errors', outcome)
        self.assertNotEqual(outcome['result'], 'BROWSE_SCALING_LOAD_OK')

    def test_missing_or_foreign_audit_is_not_replaced_with_current_metrics(self):
        with patch.object(s, 'DURABLE_EVIDENCE', True), \
             patch.object(s.evidence, 'read', side_effect=ValueError('EVIDENCE_IDENTITY_MISMATCH')):
            with self.assertRaisesRegex(ValueError, 'IDENTITY_MISMATCH'):
                s.saved_observation(JOB, RUN)


if __name__ == '__main__':
    unittest.main()
