#!/usr/bin/env bash
# User-run Cloud Shell helper for reviewed administrator SQL files only.
set +x
set -euo pipefail

if [[ $# != 1 || ! -r "$1" || ! -s "$1" ]]; then
  echo 'Usage: bash run-postgres-admin-sql.sh /path/to/reviewed-file.sql' >&2
  exit 1
fi
sql_file=$1
expected_subscription=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
postgres_host=trafficxx-pg-e8708a85.postgres.database.azure.com
pod="trafficxx-pg-admin-$(date +%s)-$RANDOM"
created=false

for command in az kubectl; do
  command -v "$command" >/dev/null || { echo "Missing tool: $command" >&2; exit 1; }
done
[[ "$(az account show --query id -o tsv)" == "$expected_subscription" ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}

ca_bundle=${TRAFFICXX_CA_BUNDLE:-}
if [[ -z "$ca_bundle" ]]; then
  for candidate in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    if [[ -s "$candidate" && -r "$candidate" ]]; then
      ca_bundle=$candidate
      break
    fi
  done
fi
if [[ -z "$ca_bundle" || ! -r "$ca_bundle" || ! -s "$ca_bundle" ]] ||
  ! grep -q -- '-----BEGIN CERTIFICATE-----' "$ca_bundle"; then
  echo 'No readable public PEM CA bundle found; set TRAFFICXX_CA_BUNDLE.' >&2
  exit 1
fi
postgres_user=$(az ad signed-in-user show --query userPrincipalName -o tsv)
[[ -n "$postgres_user" ]] || { echo 'Missing administrator principal name.' >&2; exit 1; }

cleanup() {
  if [[ "$created" == true ]]; then
    kubectl delete pod "$pod" -n default --ignore-not-found --wait=false >/dev/null || true
  fi
}
trap cleanup EXIT

echo 'Starting a temporary administrator SQL client on an apps node...'
kubectl run "$pod" -n default --image=postgres:17 --restart=Never \
  --labels=app=trafficxx-postgres-admin \
  --env="PGHOST=$postgres_host" --env=PGPORT=5432 \
  --env=PGDATABASE=postgres --env="PGUSER=$postgres_user" \
  --env=PGSSLMODE=verify-full --env=PGSSLROOTCERT=/tmp/trafficxx-ca.crt \
  --env=PGCONNECT_TIMEOUT=10 \
  --overrides='{"spec":{"activeDeadlineSeconds":600,"nodeSelector":{"agentpool":"apps"},"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":999,"runAsGroup":999}}}' \
  --command -- sleep 600
created=true
kubectl wait -n default --for=condition=Ready "pod/$pod" --timeout=120s

kubectl exec -n default -i "$pod" -- sh -c \
  'umask 077; cat > "$PGSSLROOTCERT"' < "$ca_bundle"
kubectl exec -n default -i "$pod" -- sh -c \
  'umask 077; cat > /tmp/trafficxx-admin.sql' < "$sql_file"
kubectl exec -n default "$pod" -- sh -c '
  set -eu
  test -s "$PGSSLROOTCERT" && test -r "$PGSSLROOTCERT"
  test -s /tmp/trafficxx-admin.sql && test -r /tmp/trafficxx-admin.sql
  command -v psql >/dev/null
'

echo 'Running the reviewed SQL with verified TLS; the access token is not displayed.'
# SQL files may contain psql commands and run as the human administrator.
# Only run reviewed files; never include tokens/passwords in the SQL itself.
az account get-access-token --resource-type oss-rdbms \
  --query accessToken -o tsv --only-show-errors |
  kubectl exec -n default -i "$pod" -- sh -c '
    set -eu
    IFS= read -r PGPASSWORD
    test -n "$PGPASSWORD" || { echo "No access token received" >&2; exit 1; }
    export PGPASSWORD
    exec psql -X -w -v ON_ERROR_STOP=1 --file=/tmp/trafficxx-admin.sql
  '
echo 'SQL completed; removing the temporary administrator client.'
