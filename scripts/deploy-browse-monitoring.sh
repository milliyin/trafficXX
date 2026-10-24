#!/usr/bin/env bash
# Config-only update: no Grafana deployment, credentials or HTTPS settings touched.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl rollout status deployment/browse -n trafficxx --timeout=120s
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=120s
kubectl rollout status deployment/grafana -n trafficxx-monitoring --timeout=120s
kubectl apply -f kubernetes/monitoring/browse-metrics-service.yaml
kubectl create configmap prometheus-config -n trafficxx-monitoring \
  --from-file=prometheus.yml=monitoring/prometheus/prometheus.yml \
  --from-file=browse-alerts.yml=monitoring/prometheus/browse-alerts.yml \
  --dry-run=client -o yaml | kubectl apply -f -
# Existing collector validates the new config with promtool on startup.
kubectl rollout restart deployment/prometheus -n trafficxx-monitoring
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=600s
kubectl create configmap grafana-dashboards -n trafficxx-monitoring \
  --from-file=trafficxx-operations.json=monitoring/grafana/trafficxx-operations.json \
  --from-file=trafficxx-browse-pool.json=monitoring/grafana/trafficxx-browse-pool.json \
  --from-file=trafficxx-booking-pool.json=monitoring/grafana/trafficxx-booking-pool.json \
  --dry-run=client -o yaml | kubectl apply -f -
echo 'BROWSE_MONITORING_CONFIGURED; allow ConfigMap/provider refresh, then verify stored samples and dashboard.'
