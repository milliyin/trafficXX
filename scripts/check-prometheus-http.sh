#!/usr/bin/env bash
# Read-only monitoring check through a temporary localhost-only port forward.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --browse-pool && "$1" != --browse-alerts ) ]]; then
  echo 'Usage: check-prometheus-http.sh [--browse-pool|--browse-alerts]' >&2; exit 2;
fi
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
forward_log=$(mktemp "${TMPDIR:-/tmp}/trafficxx-prometheus-forward.XXXXXX")
kubectl port-forward -n trafficxx-monitoring --address=127.0.0.1 service/prometheus 18090:9090 > "$forward_log" 2>&1 &
forward_pid=$!
cleanup() {
  status=$?
  kill "$forward_pid" 2>/dev/null || true
  wait "$forward_pid" 2>/dev/null || true
  if [[ "$status" != 0 ]]; then cat "$forward_log" >&2; fi
  rm -f -- "$forward_log"
}
trap cleanup EXIT
for attempt in {1..80}; do
  if ! kill -0 "$forward_pid" 2>/dev/null; then
    echo 'Port forward exited before becoming ready.' >&2; exit 1
  fi
  if grep -q 'Forwarding from 127.0.0.1:18090' "$forward_log"; then break; fi
  sleep 0.25
done
grep -q 'Forwarding from 127.0.0.1:18090' "$forward_log" || {
  echo 'Port forward did not become ready.' >&2; exit 1;
}
if [[ "${1:-}" == --browse-alerts ]]; then
  python3 scripts/check-browse-alerts.py
elif [[ "${1:-}" == --browse-pool ]]; then
  python3 scripts/check-browse-monitoring.py
else
  python3 scripts/check-prometheus-http.py
fi
