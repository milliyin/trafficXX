import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


def load():
    spec = importlib.util.spec_from_file_location('cc_restore', Path(__file__).resolve().parents[1]/'scripts/check-clickclash-restore.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SUCCESS = {'result': 'CLICKCLASH_LOGICAL_RESTORE_OK', 'players': 1, 'score': 3, 'receipts': 3,
           'image_restored': True, 'session_restored': True, 'audit_sequence_verified': True,
           'scratch_database_removed': True, 'production_writes': False, 'managed_pitr_tested': False}


class ClickClashRestoreTests(unittest.TestCase):
    def test_pod_is_private_bounded_and_verifies_tls(self):
        pod = load().pod_manifest('fixture', 'operator-fixture')['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['activeDeadlineSeconds'], 600)
        self.assertEqual(pod['volumes'][0]['emptyDir']['medium'], 'Memory')
        container = pod['containers'][0]
        env = {item['name']:item['value'] for item in container['env']}
        self.assertEqual(env['PGSSLMODE'], 'verify-full')
        self.assertNotIn('PGPASSWORD', env)
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])

    def test_failures_and_extra_fields_cannot_leak_credentials(self):
        module = load()
        result = module.safe_outcome({'result': 'PRIVATE_TOKEN', 'stage': 'PRIVATE_TOKEN',
                                      'scratch_database_removed': 'PRIVATE_TOKEN'})
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(result))
        self.assertEqual(result['result'], 'CLICKCLASH_LOGICAL_RESTORE_FAILED')
        self.assertIsNone(result['scratch_database_removed'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(module.safe_outcome({**SUCCESS,'unexpected':'PRIVATE_TOKEN'})))

    def test_cannot_claim_success_without_all_checks_and_cleanup(self):
        module = load()
        for key in ('image_restored', 'session_restored', 'audit_sequence_verified', 'scratch_database_removed'):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                module.safe_outcome({**SUCCESS,key:False})
        with self.assertRaises(RuntimeError):
            module.safe_outcome({**SUCCESS,'players':'1'})

    def test_human_token_only_goes_to_stdin_and_pod_is_removed(self):
        module = load()
        calls = []
        def call(args, payload=None):
            calls.append((args,payload))
            if args[:3] == ['az','ad','signed-in-user']:
                return 'f7edc6d0-ccf5-46cc-9877-fd012e5e6a87' if args[5]=='id' else 'operator-fixture'
            if args[:3] == ['az','account','get-access-token']: return 'PRIVATE_TOKEN'
            return ''
        release = SimpleNamespace(context=Mock(),call=call)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'scripts').mkdir()
            (root/'scripts/clickclash-restore-drill.sh').write_text('# fixture')
            ca=root/'ca.crt'; ca.write_text('-----BEGIN CERTIFICATE-----\nPUBLIC_FIXTURE\n')
            with patch.object(module,'ROOT',root), patch.object(module,'load_release',return_value=release), \
                 patch.dict(module.os.environ,{'TRAFFICXX_CA_BUNDLE':str(ca)}), \
                 patch.object(module.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(SUCCESS),'')) as run, \
                 patch('sys.stdout',output):
                self.assertEqual(module.main(),0)
        self.assertEqual(run.call_args.kwargs['input'],'PRIVATE_TOKEN\n')
        self.assertNotIn('PRIVATE_TOKEN',str(run.call_args.args))
        self.assertNotIn('PRIVATE_TOKEN',str(calls))
        self.assertNotIn('PRIVATE_TOKEN',output.getvalue())
        self.assertTrue(any(args[:3]==['kubectl','delete','pod'] for args,_ in calls))
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['temporary_pod_removed'])

    def test_public_site_refusal_happens_before_operator_pod(self):
        module=load()
        def call(args,payload=None):
            if args[:3]==['az','ad','signed-in-user']: return 'f7edc6d0-ccf5-46cc-9877-fd012e5e6a87'
            return 'service/clickclash-public'
        release=SimpleNamespace(context=Mock(),call=Mock(side_effect=call))
        with patch.object(module,'load_release',return_value=release), self.assertRaises(ValueError):
            module.main()
        self.assertFalse(any(entry.args[0][:2]==['kubectl','apply'] for entry in release.call.call_args_list))
