import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load():
    spec = importlib.util.spec_from_file_location('cc_plan', Path(__file__).resolve().parents[1] / 'scripts/plan-clickclash.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def change(address, actions, mode='managed'):
    return {'address': address, 'mode': mode, 'change': {'actions': actions, 'after': {'private': 'NEVER_PRINT'}}}


class ClickClashPlanTests(unittest.TestCase):
    def test_four_scoped_additions_and_existing_noops(self):
        module = load()
        plan = {'resource_changes': [change(address, ['create']) for address in module.ADDITIONS] +
                [change('azurerm_kubernetes_cluster.lab', ['no-op'])]}
        result = module.review(plan)
        self.assertEqual(result['create'], 4)
        self.assertEqual(result['destroy'], 0)
        self.assertNotIn('NEVER_PRINT', json.dumps(result))

    def test_rejects_existing_service_updates_deletes_and_foreign_additions(self):
        module = load()
        for actions in (['update'], ['delete'], ['delete', 'create'], ['create']):
            result = module.review({'resource_changes': [change('azurerm_kubernetes_cluster.lab', actions)]})
            self.assertEqual(result['result'], 'CLICKCLASH_PLAN_REJECTED')
            self.assertNotIn('NEVER_PRINT', json.dumps(result))

    def test_rejects_incomplete_plan(self):
        module = load()
        for plan in ({'errored': True}, {'complete': False}, {'deferred_changes': [{}]}):
            with self.assertRaises(ValueError):
                module.review(plan)

    def test_missing_change_metadata_is_not_a_success(self):
        with self.assertRaises(ValueError):
            load().review({})

    def test_fresh_checkout_restores_flags_and_never_applies(self):
        module = load()
        seen = []
        def fake(args, operation, environment=None):
            seen.append((args, environment))
            if operation == 'azure_account': return module.SUBSCRIPTION
            if operation == 'azure_ad': return module.ADMIN if 'id' in args else 'private-admin-name'
            if operation == 'azure_aks': return 'true'
            if operation == 'terraform_version': return '{"terraform_version":"1.16.6"}'
            if operation == 'terraform_state_addresses': return '\n'.join(module.REQUIRED_STATE)
            if operation == 'terraform_plan_review': return json.dumps({'resource_changes': [change(a, ['create']) for a in module.ADDITIONS]})
            return ''
        with tempfile.TemporaryDirectory() as target, patch.object(module, 'FOUNDATION', Path(target)), \
             patch.object(module, 'call', side_effect=fake), patch('builtins.print'):
            self.assertEqual(module.main(), 0)
            settings = json.loads((Path(target)/'clickclash-enabled.auto.tfvars.json').read_text())
        self.assertTrue(all(settings.values()))
        self.assertFalse(any('apply' in args for args, _ in seen))
        planned = next(args for args, _ in seen if 'plan' in args)
        self.assertIn('-var=enable_browse_keda=true', planned)
        self.assertIn('-var=enable_grafana_public_endpoint=true', planned)
        init = next(args for args, _ in seen if 'init' in args)
        self.assertIn('-backend-config=key=lab/foundation.tfstate', init)
