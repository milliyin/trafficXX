#!/usr/bin/env bash
# User-run Cloud Shell deployment of the existing, digest-pinned runtime image.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl apply -f kubernetes/apps/publisher-deployment.yaml
kubectl rollout status deployment/publisher -n trafficxx --timeout=600s
kubectl get pods -n trafficxx -l app.kubernetes.io/name=trafficxx-publisher -o wide
echo 'PUBLISHER_DEPLOYED; next verify persisted outbox drain and live queue state.'
