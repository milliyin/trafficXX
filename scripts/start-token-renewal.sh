#!/usr/bin/env bash
# Start one persistent diagnostic process; no long Cloud Shell wait or image build.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
[[ -z "$(kubectl get job browse-token-renewal-check -n trafficxx --ignore-not-found -o name)" ]] || {
  echo 'Renewal Job already exists. Inspect its logs/status; do not overwrite running evidence.' >&2; exit 1;
}
reference=$(kubectl get deployment browse -n trafficxx -o 'jsonpath={.spec.template.spec.containers[?(@.name=="browse")].image}')
[[ "$reference" =~ ^trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Browse must use an immutable lab image first.' >&2; exit 1;
}
kubectl create configmap browse-token-renewal-files -n trafficxx \
  --from-file=check-token-renewal.py=scripts/check-token-renewal.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl set image --local -f kubernetes/checks/token-renewal.yaml \
  "check=$reference" -o yaml | kubectl create -f -
for attempt in {1..60}; do
  logs=$(kubectl logs -n trafficxx job/browse-token-renewal-check -c check --tail=5 2>/dev/null || true)
  if [[ "$logs" == *'"result": "DATABASE_TOKEN_RENEWAL_OK"'* ]]; then
    printf '%s\n' "$logs"
    echo 'TOKEN_RENEWAL_FINISHED; verify Job completion status and retain the result.'
    exit 0
  fi
  if [[ "$logs" == *'"result": "TOKEN_RENEWAL_WAITING"'* ]]; then
    printf '%s\n' "$logs"
    echo 'TOKEN_RENEWAL_STARTED; Job continues if Cloud Shell disconnects. Check logs later; this is not a renewal pass yet.'
    exit 0
  fi
  if [[ "$logs" == *'"result": "DATABASE_TOKEN_RENEWAL_FAILED"'* ]]; then
    printf '%s\n' "$logs"; exit 1
  fi
  sleep 2
done
echo 'Initial SQL proof not available yet. Inspect job/browse-token-renewal-check; do not create a replacement blindly.' >&2
exit 1
