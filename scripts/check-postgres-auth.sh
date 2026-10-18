#!/usr/bin/env bash
# Run personally in Azure Cloud Shell. Do not enable tracing around access tokens.
set +x
set -euo pipefail

expected_subscription=35aaf788-f4cf-40c3-9ea5-bc8fe8708a85
expected_context=trafficxx-lab-aks
postgres_host=trafficxx-pg-e8708a85.postgres.database.azure.com
namespace=default
pod="trafficxx-pg-auth-$(date +%s)-$RANDOM"
created=false

# postgres:17 does not guarantee a system CA bundle. Use the trusted public CA
# bundle from Cloud Shell and copy it into the non-root diagnostic pod instead.
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
  echo 'No readable PEM CA bundle found. Set TRAFFICXX_CA_BUNDLE to a trusted CA bundle path.' >&2
  exit 1
fi

for command in az kubectl; do
  command -v "$command" >/dev/null || { echo "Missing tool: $command" >&2; exit 1; }
done

if [[ "$(az account show --query id --output tsv)" != "$expected_subscription" ]]; then
  echo 'Select the trafficXX sponsorship subscription before running this check.' >&2
  exit 1
fi
if [[ "$(kubectl config current-context)" != "$expected_context" ]]; then
  echo 'Select the trafficxx-lab-aks Kubernetes context before running this check.' >&2
  exit 1
fi

postgres_user=$(az ad signed-in-user show --query userPrincipalName --output tsv)
if [[ -z "$postgres_user" ]]; then
  echo 'Could not determine the signed-in administrator principal name.' >&2
  exit 1
fi

cleanup() {
  if [[ "$created" == true ]]; then
    kubectl delete pod "$pod" --namespace "$namespace" \
      --ignore-not-found --wait=false >/dev/null || true
  fi
}
trap cleanup EXIT

echo 'Starting a temporary PostgreSQL client on an application node...'
kubectl run "$pod" --namespace "$namespace" \
  --image=postgres:17 --restart=Never \
  --labels=app=trafficxx-postgres-auth-check \
  --env="PGHOST=$postgres_host" --env=PGPORT=5432 \
  --env=PGDATABASE=trafficxx --env="PGUSER=$postgres_user" \
  --env=PGSSLMODE=verify-full \
  --env=PGSSLROOTCERT=/tmp/trafficxx-ca.crt \
  --env=PGCONNECT_TIMEOUT=10 \
  --overrides='{"spec":{"nodeSelector":{"agentpool":"apps"},"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":999,"runAsGroup":999}}}' \
  --command -- sleep 600
created=true
kubectl wait --namespace "$namespace" --for=condition=Ready "pod/$pod" --timeout=120s

echo 'Copying the public CA trust bundle and checking the client before requesting a token...'
kubectl exec --namespace "$namespace" -i "$pod" -- sh -c \
  'umask 077; cat > "$PGSSLROOTCERT"' < "$ca_bundle"
kubectl exec --namespace "$namespace" "$pod" -- sh -c '
  set -eu
  test -s "$PGSSLROOTCERT" && test -r "$PGSSLROOTCERT" || {
    echo "Missing CA trust bundle" >&2; exit 1;
  }
  command -v psql >/dev/null
'

echo 'Checking Entra login and certificate-verified TLS (token is not displayed)...'
# The token travels over stdin to one exec process. It is not included in pod
# configuration, command arguments, a Kubernetes Secret, or a temporary file.
az account get-access-token --resource-type oss-rdbms \
  --query accessToken --output tsv --only-show-errors |
  kubectl exec --namespace "$namespace" -i "$pod" -- sh -c '
    set -eu
    IFS= read -r PGPASSWORD
    test -n "$PGPASSWORD" || { echo "No access token received" >&2; exit 1; }
    export PGPASSWORD
    exec psql --no-psqlrc --no-password --set=ON_ERROR_STOP=1 \
      --command="SELECT current_database() AS database_name, current_user AS identity, ssl, version AS tls_version FROM pg_stat_ssl WHERE pid = pg_backend_pid();"
  '

echo 'Authenticated SQL check passed; removing the temporary client pod.'
