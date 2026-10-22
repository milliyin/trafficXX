#!/usr/bin/env bash
# User-run private lab collector deployment; retains metrics in a dedicated PVC.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(az account show --query id -o tsv)" == 35aaf788-f4cf-40c3-9ea5-bc8fe8708a85 ]] || {
  echo 'Select the trafficXX sponsorship subscription first.' >&2; exit 1;
}
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
[[ "$(kubectl get storageclass managed-csi -o jsonpath='{.provisioner}')" == disk.csi.azure.com ]] || {
  echo 'Expected managed-csi Azure Disk storage class; inspect before deploying.' >&2; exit 1;
}
kubectl rollout status deployment/operations -n trafficxx --timeout=120s
kubectl apply -f kubernetes/monitoring/namespace.yaml
existing=$(kubectl get deployment/prometheus -n trafficxx-monitoring --ignore-not-found -o name)
kubectl create configmap prometheus-config -n trafficxx-monitoring \
  --from-file=prometheus.yml=monitoring/prometheus/prometheus.yml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f kubernetes/monitoring/prometheus.yaml
# The static ConfigMap is revalidated by promtool at every pod start.
if [[ -n "$existing" ]]; then
  kubectl rollout restart deployment/prometheus -n trafficxx-monitoring
fi
kubectl rollout status deployment/prometheus -n trafficxx-monitoring --timeout=600s
kubectl get pods,pvc -n trafficxx-monitoring -o wide
echo 'PROMETHEUS_DEPLOYED; next run scripts/check-prometheus-http.sh.'
