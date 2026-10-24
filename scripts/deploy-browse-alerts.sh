#!/usr/bin/env bash
# Config-only alert-rule update; renewal Job and serving workloads are untouched.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=120s
kubectl create configmap prometheus-config -n trafficxx-monitoring \
  --from-file=prometheus.yml=monitoring/prometheus/prometheus.yml \
  --from-file=browse-alerts.yml=monitoring/prometheus/browse-alerts.yml \
  --from-file=booking-alerts.yml=monitoring/prometheus/booking-alerts.yml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/prometheus -n trafficxx-monitoring
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=600s
echo 'BROWSE_ALERT_RULES_DEPLOYED; verify live rule evaluation. External notifications are not configured.'
