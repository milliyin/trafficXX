#!/usr/bin/env bash
# Dashboard ConfigMap only; Grafana polls projected files without a restart.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl rollout status deployment/grafana -n trafficxx-monitoring --timeout=120s
kubectl create configmap grafana-dashboards -n trafficxx-monitoring \
  --from-file=trafficxx-operations.json=monitoring/grafana/trafficxx-operations.json \
  --from-file=trafficxx-browse-pool.json=monitoring/grafana/trafficxx-browse-pool.json \
  --dry-run=client -o yaml | kubectl apply -f -
echo 'BROWSE_DASHBOARD_UPDATED; allow projected-file/provider refresh, then verify --browse-pool.'
