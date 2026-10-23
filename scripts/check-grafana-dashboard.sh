#!/usr/bin/env bash
# User-run check inside AKS; bootstrap credentials never printed.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --browse-pool ) ]]; then
  echo 'Usage: check-grafana-dashboard.sh [--browse-pool]' >&2; exit 2;
fi
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
kubectl create configmap grafana-dashboard-check-files -n trafficxx-monitoring \
  --from-file=check-grafana-dashboard.py=scripts/check-grafana-dashboard.py \
  --from-file=check-browse-monitoring.py=scripts/check-browse-monitoring.py \
  --dry-run=client -o yaml | kubectl apply -f -
if [[ "${1:-}" == --browse-pool ]]; then
  kubectl patch --local -f kubernetes/checks/grafana-dashboard.yaml --type=json \
    -p '[{"op":"replace","path":"/metadata/name","value":"grafana-browse-pool-check"},{"op":"replace","path":"/spec/activeDeadlineSeconds","value":360},{"op":"add","path":"/spec/template/spec/containers/0/args","value":["--browse-pool"]}]' \
    -o yaml | kubectl create -f -
  job_name=grafana-browse-pool-check
else
  kubectl apply -f kubernetes/checks/grafana-dashboard.yaml
  job_name=grafana-dashboard-check
fi
kubectl wait -n trafficxx-monitoring --for=condition=complete "job/$job_name" --timeout=360s
kubectl logs -n trafficxx-monitoring "job/$job_name" -c check
