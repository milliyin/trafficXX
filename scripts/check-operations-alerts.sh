#!/usr/bin/env bash
# Controlled consumer pause/recovery while observing internal monitoring endpoints.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
forward_log=$(mktemp "${TMPDIR:-/tmp}/trafficxx-operations-forward.XXXXXX")
kubectl port-forward -n trafficxx --address=127.0.0.1 service/operations 18003:80 > "$forward_log" 2>&1 &
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
  if grep -q 'Forwarding from 127.0.0.1:18003' "$forward_log"; then break; fi
  sleep 0.25
done
grep -q 'Forwarding from 127.0.0.1:18003' "$forward_log" || {
  echo 'Port forward did not become ready.' >&2; exit 1;
}
python3 scripts/check-operations-alerts.py "$@"
