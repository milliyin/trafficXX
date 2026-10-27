"""Cloud Shell plan only: reuse the existing backend and reject unrelated changes."""
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = ROOT / 'infrastructure/foundation'
SUBSCRIPTION = '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85'
ADMIN = 'f7edc6d0-ccf5-46cc-9877-fd012e5e6a87'
ADDITIONS = {
    'azurerm_user_assigned_identity.workload["clickclash"]',
    'azurerm_federated_identity_credential.workload["clickclash"]',
    'azurerm_public_ip.clickclash[0]',
    'azurerm_role_assignment.aks_clickclash_public_ip[0]',
}
REQUIRED_STATE = {
    'azurerm_kubernetes_cluster.lab', 'azurerm_postgresql_flexible_server.lab',
    'azurerm_servicebus_namespace.lab', 'azurerm_public_ip.grafana[0]',
    'azurerm_user_assigned_identity.workload["browse"]',
}


class PlanFailure(RuntimeError):
    def __init__(self, operation, code=None):
        self.operation, self.code = operation, code


def call(args, operation, environment=None):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=900, env=environment)
    except FileNotFoundError:
        raise PlanFailure(operation, 127) from None
    except subprocess.TimeoutExpired:
        raise PlanFailure(operation) from None
    if result.returncode:
        raise PlanFailure(operation, result.returncode)
    return result.stdout.strip()


def review(plan):
    if plan.get('errored') or plan.get('complete') is False or plan.get('deferred_changes'):
        raise ValueError('INCOMPLETE_PLAN')
    if not isinstance(plan.get('resource_changes'), list):
        raise ValueError('INVALID_PLAN_METADATA')
    additions, unexpected = [], []
    for resource in plan.get('resource_changes', []):
        actions = resource['change']['actions']
        if actions == ['no-op'] or (resource.get('mode') == 'data' and actions == ['read']):
            continue
        address = resource['address']
        if resource.get('mode', 'managed') == 'managed' and actions == ['create'] and address in ADDITIONS:
            additions.append(address)
        else:
            unexpected.append({'address': address, 'actions': actions})
    if unexpected:
        # Addresses/actions only; never expose planned values, outputs or state.
        return {'result': 'CLICKCLASH_PLAN_REJECTED', 'unexpected_changes': unexpected,
                'note': 'Do not apply. Existing services must remain unchanged.'}
    return {'result': 'CLICKCLASH_PLAN_ONLY_OK', 'create': len(additions), 'update': 0, 'destroy': 0,
            'new_resources': sorted(additions)}


def main():
    def az(*args):
        return call(['az', *args], 'azure_' + args[0])
    if az('account', 'show', '--query', 'id', '-o', 'tsv') != SUBSCRIPTION:
        raise ValueError('WRONG_SUBSCRIPTION')
    if az('ad', 'signed-in-user', 'show', '--query', 'id', '-o', 'tsv') != ADMIN:
        raise ValueError('WRONG_LAB_ADMINISTRATOR')
    principal_name = az('ad', 'signed-in-user', 'show', '--query', 'userPrincipalName', '-o', 'tsv')
    if not principal_name:
        raise ValueError('MISSING_ADMINISTRATOR_NAME')
    if az('aks', 'show', '-g', 'trafficxx-lab-rg', '-n', 'trafficxx-lab-aks',
          '--query', 'workloadAutoScalerProfile.keda.enabled', '-o', 'tsv').lower() != 'true':
        raise ValueError('EXISTING_KEDA_NOT_ENABLED')

    environment = {**os.environ, 'ARM_SUBSCRIPTION_ID': SUBSCRIPTION,
                   'TF_VAR_subscription_id': SUBSCRIPTION, 'TF_VAR_location': 'eastus',
                   'TF_VAR_registry_name': 'trafficxxacre8708a85', 'TF_VAR_aks_admin_object_id': ADMIN,
                   'TF_VAR_postgres_admin_principal_name': principal_name}
    def terraform(operation, *args):
        return call(['terraform', '-chdir=' + str(FOUNDATION), *args], operation, environment)
    version = json.loads(terraform('terraform_version', 'version', '-json'))['terraform_version']
    if not ( (1,16,5) <= tuple(map(int, version.split('-')[0].split('.'))) < (1,17,0) ):
        raise ValueError('TERRAFORM_VERSION_MUST_BE_1_16_5_TO_1_16_X')
    print('Reconnecting to the existing foundation backend...', flush=True)
    terraform('terraform_init', 'init', '-input=false',
              '-backend-config=resource_group_name=trafficxx-tfstate-rg',
              '-backend-config=storage_account_name=trafficxxtfstatee8708a85',
              '-backend-config=container_name=tfstate', '-backend-config=key=lab/foundation.tfstate',
              '-backend-config=use_azuread_auth=true')
    state = set(terraform('terraform_state_addresses', 'state', 'list').splitlines())
    if not REQUIRED_STATE <= state:
        raise ValueError('EXISTING_FOUNDATION_STATE_MISSING')
    terraform('terraform_validate', 'validate', '-no-color')
    # Persist known enabled features in this new checkout. Never replace an
    # existing user settings file or rely on opt-in defaults after a shell reset.
    settings = FOUNDATION / 'clickclash-enabled.auto.tfvars.json'
    features = {'enable_clickclash': True, 'enable_browse_keda': True, 'enable_grafana_public_endpoint': True}
    if settings.is_symlink():
        raise ValueError('SETTINGS_SYMLINK_REFUSED')
    if settings.exists():
        if json.loads(settings.read_text()) != features:
            raise ValueError('EXISTING_CLICKCLASH_SETTINGS_DIFFER')
    else:
        with settings.open('x') as handle:
            json.dump(features, handle, indent=2)
            handle.write('\n')
    plan_name = 'clickclash-' + uuid4().hex[:12] + '.tfplan'
    print('Planning ClickClash additions; preserving Grafana and KEDA...', flush=True)
    terraform('terraform_plan', 'plan', '-input=false', '-out=' + plan_name,
              '-var=enable_clickclash=true', '-var=enable_browse_keda=true',
              '-var=enable_grafana_public_endpoint=true')
    report = review(json.loads(terraform('terraform_plan_review', 'show', '-json', plan_name)))
    if report['result'] != 'CLICKCLASH_PLAN_ONLY_OK':
        print(json.dumps(report))
        return 1
    print(json.dumps({**report, 'plan_file': str(FOUNDATION / plan_name),
                      'note': 'Plan only. No apply, app rollout or public listener was started.'}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PlanFailure as error:
        print(json.dumps({'result': 'CLICKCLASH_PLAN_FAILED', 'operation': error.operation, 'exit_code': error.code}))
        sys.exit(1)
    except (ValueError, KeyError) as error:
        allowed = {'INCOMPLETE_PLAN', 'WRONG_SUBSCRIPTION', 'WRONG_LAB_ADMINISTRATOR',
                   'MISSING_ADMINISTRATOR_NAME', 'EXISTING_KEDA_NOT_ENABLED',
                   'TERRAFORM_VERSION_MUST_BE_1_16_5_TO_1_16_X', 'EXISTING_FOUNDATION_STATE_MISSING',
                   'SETTINGS_SYMLINK_REFUSED', 'EXISTING_CLICKCLASH_SETTINGS_DIFFER'}
        print(json.dumps({'result': 'CLICKCLASH_PLAN_FAILED', 'check': str(error) if str(error) in allowed else 'INVALID_METADATA'}))
        sys.exit(1)
