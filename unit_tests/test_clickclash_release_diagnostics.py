import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


def load():
    spec = importlib.util.spec_from_file_location('cc_diagnostics', Path(__file__).resolve().parents[1] / 'scripts/clickclash-azure.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseDiagnostics(unittest.TestCase):
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
