"""Rotation uses fake credentials/APIs and temporary files; no Azure access."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'grafana_rotation', Path(__file__).resolve().parents[1] / 'scripts/rotate-grafana-password.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Rotation(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / '.env.grafana'
        self.pending = self.path.with_name('.env.grafana.next')
        self.original = 'A' * 48
        self.password = self.original
        self.sessions = [{'id': 3}]
        self.identity = {'id': 7, 'login': 'admin', 'isGrafanaAdmin': True}
        self.secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
            'name': 'grafana-admin', 'namespace': 'trafficxx-monitoring', 'resourceVersion': '123',
            'annotations': {'kubectl.kubernetes.io/last-applied-configuration': 'stale'}},
            'data': {key: base64.b64encode(value.encode()).decode() for key, value in {
                'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': self.original}.items()}}
        self.calls = []
        self.fail_replace = False
        self.fail_logout = False
        self.keep_sessions = False
        self.keep_old = False

    def call(self, args, payload=None):
        self.calls.append((args, payload))
        if args[1] == 'get':
            return json.dumps(self.secret)
        self.assertEqual(args, ['kubectl', 'replace', '-f', '-'])
        if self.fail_replace:
            raise RuntimeError('fake replacement failure')
        self.secret = json.loads(payload)
        return 'replaced'

    def request(self, method, path, password, payload=None):
        if password != self.password and not (self.keep_old and password == self.original):
            raise module.Unauthorized()
        if path == '/api/user':
            return copy.deepcopy(self.identity)
        self.assertTrue(path.startswith('/api/admin/users/7/'))
        if method == 'PUT' and path.endswith('/password'):
            self.password = payload['password']
            return {'message': 'updated'}
        if method == 'POST' and path.endswith('/logout'):
            if self.fail_logout:
                raise RuntimeError('fake logout failure')
            if not self.keep_sessions:
                self.sessions = []
            return {'message': 'revoked'}
        if path.endswith('/auth-tokens'):
            return copy.deepcopy(self.sessions)
        raise AssertionError('Unexpected API operation')

    def run_rotation(self):
        return module.rotate(self.call, self.request, self.path)

    def test_success_changes_persisted_password_revokes_sessions_and_syncs_without_disclosure(self):
        result = self.run_rotation()
        self.assertNotEqual(self.password, self.original)
        self.assertEqual(result['result'], 'GRAFANA_PASSWORD_ROTATED')
        self.assertEqual(self.sessions, [])
        self.assertEqual(module.credentials.read_file(self.path)['GF_SECURITY_ADMIN_PASSWORD'], self.password)
        self.assertEqual(base64.b64decode(self.secret['data']['GF_SECURITY_ADMIN_PASSWORD']).decode(), self.password)
        self.assertFalse(self.pending.exists())
        self.assertNotIn(self.password, json.dumps(result))
        self.assertNotIn(self.original, json.dumps(result))
        self.assertNotIn('kubectl.kubernetes.io/last-applied-configuration', self.secret['metadata']['annotations'])
        self.assertEqual(self.secret['metadata']['resourceVersion'], '123')
        for args, _ in self.calls:
            self.assertNotIn(self.password, str(args))
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_interrupted_secret_sync_resumes_using_same_pending_password(self):
        self.fail_replace = True
        with self.assertRaises(RuntimeError): self.run_rotation()
        changed = self.password
        self.assertTrue(self.pending.exists())
        self.assertEqual(module.credentials.read_file(self.path)['GF_SECURITY_ADMIN_PASSWORD'], self.original)
        self.fail_replace = False
        result = self.run_rotation()
        self.assertEqual(self.password, changed)
        self.assertTrue(result['secret_synchronized'])

    def test_interrupted_after_secret_replace_resumes_stale_local_file(self):
        self.run_rotation()
        new = module.credentials.read_file(self.path)
        module.write_private(self.pending, new)
        self.path.write_text('GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD=' + self.original + '\n')
        self.run_rotation()
        self.assertEqual(module.credentials.read_file(self.path), new)

    def test_failed_revocation_never_replaces_secret(self):
        self.fail_logout = True
        with self.assertRaises(RuntimeError): self.run_rotation()
        self.assertTrue(self.pending.exists())
        self.assertFalse(any(args[1] == 'replace' for args, _ in self.calls))

    def test_remaining_sessions_stop_before_secret_sync(self):
        self.keep_sessions = True
        with self.assertRaises(ValueError): self.run_rotation()
        self.assertFalse(any(args[1] == 'replace' for args, _ in self.calls))

    def test_nonadmin_or_stale_secret_rejected_before_password_mutation(self):
        self.identity['isGrafanaAdmin'] = False
        with self.assertRaises(ValueError): self.run_rotation()
        self.assertEqual(self.password, self.original)
        self.assertFalse(self.pending.exists())
        self.identity['isGrafanaAdmin'] = True
        self.password = 'B' * 48
        with self.assertRaises(module.Unauthorized): self.run_rotation()
        self.assertFalse(self.pending.exists())

    def test_mismatched_local_file_is_not_overwritten(self):
        values = {'GF_SECURITY_ADMIN_USER': 'admin', 'GF_SECURITY_ADMIN_PASSWORD': 'C' * 48}
        module.write_private(self.path, values)
        with self.assertRaises(ValueError): self.run_rotation()
        self.assertEqual(module.credentials.read_file(self.path), values)
        self.assertEqual(self.password, self.original)

    def test_old_password_still_accepted_stops_before_publication_marker(self):
        self.keep_old = True
        with self.assertRaises(ValueError): self.run_rotation()
        self.assertTrue(self.pending.exists())
        self.assertFalse(any(args[1] == 'replace' for args, _ in self.calls))

    def test_redirect_handler_does_not_forward_authorization(self):
        self.assertIsNone(module.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))
