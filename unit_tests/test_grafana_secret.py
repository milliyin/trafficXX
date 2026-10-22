"""Credential creation/reuse uses stdin/private files, never printed values."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'grafana_secret', Path(__file__).resolve().parents[1] / 'scripts/ensure-grafana-secret.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GrafanaSecret(unittest.TestCase):
    def test_new_secret_uses_stdin_and_private_file_without_outputting_password(self):
        calls = []

        def call(args, payload=None):
            calls.append((args, payload))
            return ''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env.grafana'
            result = module.ensure(call, path)
            values = module.read_file(path)
            secret = json.loads(calls[1][1])
            self.assertEqual(base64.b64decode(secret['data']['GF_SECURITY_ADMIN_PASSWORD']).decode(),
                             values['GF_SECURITY_ADMIN_PASSWORD'])
            self.assertGreaterEqual(len(values['GF_SECURITY_ADMIN_PASSWORD']), 32)
            self.assertNotIn(values['GF_SECURITY_ADMIN_PASSWORD'], json.dumps(result))
            self.assertNotIn(values['GF_SECURITY_ADMIN_PASSWORD'], str(calls[1][0]))
            self.assertEqual(calls[1][0], ['kubectl', 'create', '-f', '-'])
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def existing(self):
        return {'data': {key: base64.b64encode(value.encode()).decode() for key, value in {
            'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': 'A' * 48}.items()}}

    def test_existing_secret_restores_lost_cloud_shell_file_without_rotation(self):
        calls = []

        def call(args, payload=None):
            calls.append(args)
            return json.dumps(self.existing())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env.grafana'
            result = module.ensure(call, path)
            self.assertEqual(module.read_file(path)['GF_SECURITY_ADMIN_PASSWORD'], 'A' * 48)
            self.assertTrue(result['existing_secret_reused'])
            self.assertEqual(len(calls), 1)

    def test_existing_matching_file_is_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env.grafana'
            path.write_text('GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD=' + 'A' * 48 + '\n')
            module.ensure(lambda *args: json.dumps(self.existing()), path)
            self.assertEqual(module.read_file(path)['GF_SECURITY_ADMIN_PASSWORD'], 'A' * 48)

    def test_mismatch_does_not_overwrite_local_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env.grafana'
            original = 'GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD=' + 'B' * 48 + '\n'
            path.write_text(original)
            with self.assertRaises(ValueError):
                module.ensure(lambda *args: json.dumps(self.existing()), path)
            self.assertEqual(path.read_text(), original)

    def test_malformed_credentials_do_not_create_cloud_secret(self):
        calls = []

        def call(args, payload=None):
            calls.append(args)
            return ''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env.grafana'
            path.write_text('GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD=weak\n')
            with self.assertRaises(ValueError):
                module.ensure(call, path)
            self.assertEqual(len(calls), 1)
