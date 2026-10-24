#!/usr/bin/env bash
# User-run check inside AKS; bootstrap credentials never printed.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --browse-pool && "$1" != --booking-pool ) ]]; then
  echo 'Usage: check-grafana-dashboard.sh [--browse-pool|--booking-pool]' >&2; exit 2;
fi
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl create configmap grafana-dashboard-check-files -n trafficxx-monitoring \
  --from-file=check-grafana-dashboard.py=scripts/check-grafana-dashboard.py \
  --from-file=check-browse-monitoring.py=scripts/check-browse-monitoring.py \
  --from-file=check-browse-alerts.py=scripts/check-browse-alerts.py \
  --dry-run=client -o yaml | kubectl apply -f -
if [[ -n "${1:-}" ]]; then
  if [[ "$1" == --booking-pool ]]; then
    job_name=grafana-booking-pool-check
  else
    job_name=grafana-browse-pool-check
  fi
  patch='[{"op":"replace","path":"/metadata/name","value":"'"$job_name"'"},{"op":"replace","path":"/spec/activeDeadlineSeconds","value":360},{"op":"add","path":"/spec/template/spec/containers/0/args","value":["'"$1"'"]}]'
  kubectl patch --local -f kubernetes/checks/grafana-dashboard.yaml --type=json \
    -p "$patch" \
    -o yaml | kubectl create -f -
else
  kubectl apply -f kubernetes/checks/grafana-dashboard.yaml
  job_name=grafana-dashboard-check
fi
kubectl wait -n trafficxx-monitoring --for=condition=complete "job/$job_name" --timeout=360s
kubectl logs -n trafficxx-monitoring "job/$job_name" -c check
