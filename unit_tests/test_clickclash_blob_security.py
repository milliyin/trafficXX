import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from trafficxx import clickclash_blobs as blobs


class BlobBoundaryTests(unittest.TestCase):
    def test_only_generated_player_digest_keys_are_accepted(self):
        player, digest = uuid4(), 'a' * 64
        self.assertEqual(blobs.name(player, digest), f'banners/{player}/{digest}.webp')
        for invalid in ('../secret', 'https://evil.test/x', 'A' * 64, ''):
            with self.assertRaises(ValueError):
                blobs.name(player, invalid)
        with patch.object(blobs, 'container') as container:
            with self.assertRaises(blobs.StorageUnavailable):
                blobs.read('https://evil.test/x', player, digest)
            container.assert_not_called()

    def test_download_is_bounded_and_integrity_checked(self):
        raw, player = b'valid-webp-standin', uuid4()
        digest = hashlib.sha256(raw).hexdigest()
        client = MagicMock()
        client.download_blob.return_value.readall.return_value = raw
        container = MagicMock()
        container.get_blob_client.return_value = client
        with patch.object(blobs, 'container', return_value=container):
            self.assertEqual(blobs.read(blobs.name(player, digest), player, digest), raw)
            self.assertEqual(client.download_blob.call_args.kwargs['length'], 524289)
            for invalid in (b'corrupt', b'x' * 524289):
                client.download_blob.return_value.readall.return_value = invalid
                with self.assertRaises(blobs.StorageUnavailable):
                    blobs.read(blobs.name(player, digest), player, digest)

    def test_referenced_objects_cannot_be_cleaned_up(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {'exists': True}
        with patch.object(blobs, 'container') as container:
            blobs.delete_unreferenced(conn, uuid4(), 'a' * 64)
            container.assert_not_called()

    def test_storage_plan_never_accepts_old_resource_updates(self):
        spec = importlib.util.spec_from_file_location('blobplan', Path('scripts/plan-clickclash.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ADDITIONS = {'azurerm_storage_account.clickclash[0]'}
        self.assertEqual(module.review({'resource_changes': [
            {'address': 'azurerm_kubernetes_cluster.lab', 'change': {'actions': ['update']}}
        ]})['result'], 'CLICKCLASH_PLAN_REJECTED')

    def test_release_preflight_rejects_public_or_key_enabled_storage(self):
        spec = importlib.util.spec_from_file_location('blob_release', Path('scripts/clickclash-azure.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import json
        good = {'keys': False, 'public': False, 'tls': 'TLS1_2', 'https': True,
                'blob': 'https://trafficxxcce8708a85.blob.core.windows.net/'}
        with patch.object(module, 'call', side_effect=[json.dumps(good), 'None']):
            module.storage_preflight()
        for key, value in [('keys', True), ('public', True), ('tls', 'TLS1_0'), ('https', False)]:
            with patch.object(module, 'call', return_value=json.dumps({**good, key: value})):
                with self.assertRaises(ValueError):
                    module.storage_preflight()
        with patch.object(module, 'call', side_effect=[json.dumps(good), 'Blob']):
            with self.assertRaises(ValueError):
                module.storage_preflight()
