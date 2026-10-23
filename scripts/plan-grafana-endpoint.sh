#!/usr/bin/env bash
# User-run Cloud Shell plan only; reconnect to the existing foundation state.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../infrastructure/foundation"
subscription=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
[[ "$(az account show --query id -o tsv)" == "$subscription" ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
terraform version -json | python3 -c '
import json, sys
version = tuple(map(int, json.load(sys.stdin)["terraform_version"].split("-")[0].split(".")))
if not ( (1,16,5) <= version < (1,17,0) ):
    sys.exit("Use Terraform >=1.16.5 and <1.17.0, matching this foundation.")
'
export ARM_SUBSCRIPTION_ID="$subscription"
export TF_VAR_subscription_id="$subscription"
export TF_VAR_location=eastus
export TF_VAR_registry_name=trafficxxacre8708a85
export TF_VAR_aks_admin_object_id
TF_VAR_aks_admin_object_id=$(az ad signed-in-user show --query id -o tsv)
[[ "$TF_VAR_aks_admin_object_id" == f7edc6d0-ccf5-46cc-9877-fd012e5e6a87 ]] || {
  echo 'Current user differs from the recorded lab administrator; do not change its role mapping.' >&2
  exit 1
}
export TF_VAR_postgres_admin_principal_name
TF_VAR_postgres_admin_principal_name=$(az ad signed-in-user show --query userPrincipalName -o tsv)

terraform init -input=false \
  -backend-config=resource_group_name=trafficxx-tfstate-rg \
  -backend-config=storage_account_name=trafficxxtfstatee8708a85 \
  -backend-config=container_name=tfstate \
  -backend-config=key=lab/foundation.tfstate \
  -backend-config=use_azuread_auth=true

# Missing state must never turn this into a new foundation deployment.
state=$(terraform state list)
for resource in azurerm_kubernetes_cluster.lab azurerm_postgresql_flexible_server.lab azurerm_servicebus_namespace.lab; do
  [[ "$state" == *"$resource"* ]] || {
    echo "Missing expected foundation state resource: $resource. Stop and inspect backend." >&2
    exit 1
  }
done

# Persist opt-in for future plans; do not silently replace an existing settings file.
settings=grafana-endpoint.auto.tfvars
expected='enable_grafana_public_endpoint = true'
if [[ -L "$settings" ]]; then
  echo 'Endpoint settings must not be a symlink.' >&2; exit 1
elif [[ -e "$settings" ]]; then
  [[ "$(cat "$settings")" == "$expected" ]] || {
    echo 'Existing endpoint settings differ; review them before planning.' >&2; exit 1;
  }
else
  ( set -o noclobber; printf '%s\n' "$expected" > "$settings" )
fi
terraform validate
terraform plan -input=false -out=grafana-endpoint.tfplan
echo 'PLAN_ONLY: expected two additions (public IP and its scoped role), zero changes/destroys.'
echo 'Review the plan before apply. No HTTPS listener or certificate has been deployed.'
