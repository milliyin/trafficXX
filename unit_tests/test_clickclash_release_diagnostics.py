import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


def load():
    spec = importlib.util.spec_from_file_location('cc_diagnostics', Path(__file__).resolve().parents[1] / 'scripts/clickclash-azure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseDiagnostics(unittest.TestCase):
    def test_domain_switch_reuses_running_image_and_redirects_old_host(self):
        module = load()
        azure_host = 'clickclash-e8708a85.example.cloudapp.azure.com'
        image = module.SERVER + '/trafficxx@sha256:' + 'b' * 64
        application = {'spec': {'template': {'spec': {'containers': [{
            'name': 'app', 'image': image,
            'env': [{'name': 'CLICKCLASH_ORIGIN', 'value': 'https://' + azure_host}]}]}}}}
        applied = []
        def fake(args, payload=None):
            if args[:2] == ['kubectl', 'apply']:
                applied.extend(json.loads(payload)['items'])
            return ''
        with patch.object(module.sys, 'argv', ['release', 'custom-domain']), \
             patch.object(module, 'context'), patch.object(module, 'metadata', return_value=(azure_host, '20.120.14.226', {'clientId':'id','tenantId':'tenant'})), \
             patch.object(module, 'current_application', return_value=application), \
             patch.object(module, 'storage_preflight'), patch.object(module, 'domain_preflight'), \
             patch.object(module, 'call', side_effect=fake), patch('sys.stdout', io.StringIO()):
            module.main()
        app = next(item for item in applied if item['kind']=='Deployment' and item['metadata']['name']=='clickclash')
        container = app['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], image)
        self.assertIn({'name':'CLICKCLASH_ORIGIN','value':'https://clickclash.milliyin.dev'}, container['env'])
        caddy = next(item['data']['Caddyfile'] for item in applied if item['kind']=='ConfigMap')
        self.assertIn('redir https://clickclash.milliyin.dev{uri} 308', caddy)
        self.assertIn('http://clickclash.milliyin.dev, http://' + azure_host, caddy)
        gateway = next(item for item in applied if item['kind']=='Deployment' and item['metadata']['name']=='clickclash-gateway')
        self.assertIn('clickclash/caddy-config-sha256', gateway['spec']['template']['metadata']['annotations'])
        self.assertEqual(module.active_hostname(app, azure_host), module.CUSTOM_DOMAIN)

    def test_custom_domain_requires_exact_public_ip_and_no_stray_ipv6(self):
        module = load()
        records = [(2,1,6,'',('20.120.14.226',443))]
        with patch.object(module.socket, 'getaddrinfo', return_value=records):
            module.domain_preflight('20.120.14.226')
        for invalid in ([], [(2,1,6,'',('1.2.3.4',443))], records+[(10,1,6,'',('::1',443,0,0))]):
            with patch.object(module.socket, 'getaddrinfo', return_value=invalid):
                with self.assertRaises(ValueError):
                    module.domain_preflight('20.120.14.226')

    def test_only_known_origins_can_be_preserved(self):
        module = load()
        app = {'spec': {'template': {'spec': {'containers': [{'name':'app','env':[{'name':'CLICKCLASH_ORIGIN','value':'https://evil.test'}]}]}}}}
        with self.assertRaises(ValueError):
            module.active_hostname(app, 'clickclash.example.cloudapp.azure.com')

    def test_public_check_retries_connection_errors_without_skipping_tls(self):
        module = load()
        import urllib.error
        from unittest.mock import MagicMock
        host = module.CUSTOM_DOMAIN
        responses = []
        for path, body in [('/', b'ClickClash'), ('/readyz', b'{"readiness":"ready"}'), ('/api/leaderboard', b'{"entries":[]}')]:
            response = MagicMock()
            response.status = 200
            response.geturl.return_value = 'https://' + host + path
            response.read.return_value = body
            manager = MagicMock()
            manager.__enter__.return_value = response
            responses.append(manager)
        with patch.object(module.urllib.request, 'urlopen', side_effect=[urllib.error.URLError('fixture'), *responses]) as opened, \
             patch.object(module.time, 'sleep') as sleep, patch('sys.stdout', io.StringIO()):
            module.check_public_routes(host)
        sleep.assert_called_once_with(5)
        self.assertEqual(opened.call_count, 4)
        self.assertTrue(all(set(entry.kwargs) == {'timeout'} for entry in opened.call_args_list))

    def test_stage_command_applies_only_private_objects(self):
        module = load()
        output = io.StringIO()
        applied = []
        def fake(args, payload=None):
            if args[:2] == ['kubectl', 'apply']:
                applied.extend(json.loads(payload)['items'])
            return ''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'clickclash-image.txt').write_text(module.SERVER+'/trafficxx@sha256:'+'a'*64)
            with patch.object(module, 'ROOT', root), patch.object(module, 'context'), \
                 patch.object(module, 'metadata', return_value=('clickclash.example.com','20.1.2.3',{'clientId':'fixture','tenantId':'fixture'})), \
                 patch.object(module, 'call', side_effect=fake), patch.object(module, 'storage_preflight'), patch.object(module.sys, 'argv', ['release', 'stage']), patch('sys.stdout',output):
                module.main()
        self.assertEqual(len(applied), 3)
        self.assertTrue(all(item['metadata']['name']=='clickclash' for item in applied))
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['result'], 'CLICKCLASH_STAGED_PRIVATE')

    def test_private_stage_has_no_public_service_or_gateway(self):
        module = load()
        all_objects = module.deployment('clickclash.example.com', '20.1.2.3',
                                        {'clientId': 'fixture', 'tenantId': 'fixture'}, 'fixture-image')
        items = module.private_stage(all_objects)['items']
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item['metadata']['name'] == 'clickclash' for item in items))
        service = next(item for item in items if item['kind'] == 'Service')
        self.assertEqual(service['spec'].get('type', 'ClusterIP'), 'ClusterIP')

    def test_private_stage_rejects_accidental_public_service(self):
        module = load()
        objects = module.deployment('clickclash.example.com', '20.1.2.3',
                                    {'clientId': 'fixture', 'tenantId': 'fixture'}, 'fixture-image')
        next(item for item in objects['items'] if item['kind'] == 'Service' and item['metadata']['name'] == 'clickclash')['spec']['type'] = 'LoadBalancer'
        with self.assertRaises(ValueError):
            module.private_stage(objects)

    def test_doctor_only_inspects_account_registry_and_runs(self):
        module = load()
        output = io.StringIO()
        with patch.object(module.sys, 'argv', ['clickclash-azure.py', 'doctor']), \
             patch.object(module, 'call', side_effect=[module.SUBSCRIPTION, module.SERVER, '[]']) as call, \
             patch('sys.stdout', output):
            module.main()
        self.assertEqual(json.loads(output.getvalue())['result'], 'CLICKCLASH_BUILD_PREFLIGHT_OK')
        self.assertEqual([entry.args[0][:3] for entry in call.call_args_list],
                         [['az', 'account', 'show'], ['az', 'acr', 'show'], ['az', 'acr', 'task']])

    def test_failure_never_exposes_command_or_output(self):
        module = load()
        with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'PRIVATE_OUT', 'PRIVATE_ERR')):
            with self.assertRaises(module.CommandFailure) as caught:
                module.call(['az', 'acr', 'build', '--private', 'PRIVATE_ARG'], 'PRIVATE_INPUT')
        report = module.failure_report(caught.exception)
        self.assertEqual(report['command_operation'], 'acr_build')
        self.assertEqual(report['command_exit_code'], 1)
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_build_does_not_require_cloud_shell_kubeconfig(self):
        module = load()
        with patch.object(module, 'call', return_value=module.SUBSCRIPTION) as call:
            module.context(require_cluster=False)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[0][:3], ['az', 'account', 'show'])

    def test_deploy_still_requires_exact_cluster(self):
        module = load()
        with patch.object(module, 'call', side_effect=[module.SUBSCRIPTION, 'other-cluster']):
            with self.assertRaises(ValueError) as caught:
                module.context()
        self.assertEqual(module.failure_report(caught.exception)['check'], 'WRONG_KUBERNETES_CONTEXT')

    def test_timeout_and_missing_tool_are_safe(self):
        module = load()
        for error, code, timed_out in [(FileNotFoundError('PRIVATE'), 127, False),
                                      (subprocess.TimeoutExpired(['PRIVATE'], 900, output='PRIVATE'), None, True)]:
            with self.subTest(error=type(error).__name__), patch.object(module.subprocess, 'run', side_effect=error):
                with self.assertRaises(module.CommandFailure) as caught:
                    module.call(['kubectl', 'config', 'current-context'])
                report = module.failure_report(caught.exception)
                self.assertEqual(report['command_operation'], 'kubernetes_context')
                self.assertEqual(report['command_exit_code'], code)
                self.assertEqual(report['command_timed_out'], timed_out)
                self.assertNotIn('PRIVATE', json.dumps(report))

    def test_validation_only_exposes_allowlisted_reason(self):
        module = load()
        self.assertNotIn('PRIVATE', json.dumps(module.failure_report(ValueError('PRIVATE'))))
