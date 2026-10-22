#!/usr/bin/env bash
# User-run check inside AKS; bootstrap credentials never printed.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl create configmap grafana-dashboard-check-files -n trafficxx-monitoring \
  --from-file=check-grafana-dashboard.py=scripts/check-grafana-dashboard.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/checks/grafana-dashboard.yaml
kubectl wait -n trafficxx-monitoring --for=condition=complete job/grafana-dashboard-check --timeout=180s
kubectl logs -n trafficxx-monitoring job/grafana-dashboard-check -c check
