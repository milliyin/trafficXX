#!/usr/bin/env bash
# Runs only against the expected lab, using the currently deployed Booking image.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
reference=$(kubectl get deployment booking -n trafficxx -o 'jsonpath={.spec.template.spec.containers[?(@.name=="booking")].image}')
[[ "$reference" =~ ^trafficxxacre8708a85\.azurecr\.io/trafficxx@sha256:[0-9a-f]{64}$ ]] || {
  echo 'Booking must use an immutable lab image first.' >&2; exit 1;
}
kubectl set image --local -f kubernetes/checks/booking-pool.yaml \
  "check=$reference" -o yaml | kubectl create -f -
kubectl wait -n trafficxx --for=condition=complete job/booking-pool-check --timeout=180s
kubectl logs -n trafficxx job/booking-pool-check -c check
