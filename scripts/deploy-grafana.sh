#!/usr/bin/env bash
# User-run private dashboard setup; bootstrap secrets never printed.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
[[ "$(kubectl get storageclass managed-csi -o jsonpath='{.provisioner}')" == disk.csi.azure.com ]] || {
  echo 'Expected managed-csi Azure Disk storage class.' >&2; exit 1;
}
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=120s
python3 scripts/ensure-grafana-secret.py
existing=$(kubectl get deployment/grafana -n trafficxx-monitoring --ignore-not-found -o name)
kubectl create configmap grafana-provisioning -n trafficxx-monitoring \
  --from-file=datasources.yaml=monitoring/grafana/datasources.yml \
  --from-file=dashboards.yaml=monitoring/grafana/dashboards.yml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap grafana-dashboards -n trafficxx-monitoring \
  --from-file=trafficxx-operations.json=monitoring/grafana/trafficxx-operations.json \
  --from-file=trafficxx-browse-pool.json=monitoring/grafana/trafficxx-browse-pool.json \
  --from-file=trafficxx-booking-pool.json=monitoring/grafana/trafficxx-booking-pool.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/monitoring/grafana.yaml
if [[ -n "$existing" ]]; then
  kubectl rollout restart deployment/grafana -n trafficxx-monitoring
fi
kubectl rollout status deployment/grafana -n trafficxx-monitoring --timeout=600s
kubectl get pods,pvc -n trafficxx-monitoring -o wide
echo 'GRAFANA_DEPLOYED; bootstrap login saved privately in .env.grafana. Do not paste credentials.'
