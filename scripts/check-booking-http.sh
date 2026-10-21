#!/usr/bin/env bash
# User-run check through a temporary localhost-only Kubernetes port forward.
set -euo pipefail
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
forward_log=$(mktemp "${TMPDIR:-/tmp}/trafficxx-booking-forward.XXXXXX")
kubectl port-forward -n trafficxx --address=127.0.0.1 service/booking 18002:80 > "$forward_log" 2>&1 &
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
  if grep -q 'Forwarding from 127.0.0.1:18002' "$forward_log"; then break; fi
  sleep 0.25
done
grep -q 'Forwarding from 127.0.0.1:18002' "$forward_log" || {
  echo 'Port forward did not become ready.' >&2; exit 1;
}
python3 - <<'PY'
import json
import time
import urllib.error
import urllib.request
from uuid import uuid4

base = 'http://127.0.0.1:18002'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def get(path, timeout=10):
    with opener.open(base + path, timeout=timeout) as response:
        return json.load(response)

for attempt in range(30):
    try:
        if get('/healthz', timeout=1) != {'status': 'alive'}:
            raise ValueError('Unexpected health response')
        break
    except (OSError, ValueError):
        if attempt == 29:
            raise SystemExit('HTTP_CHECK_FAILED: health endpoint unavailable')
        time.sleep(0.25)
try:
    if get('/readyz') != {'status': 'ready'}:
        raise ValueError('Unexpected readiness response')
    api = get('/openapi.json')
    if (api.get('info', {}).get('title') != 'trafficXX Booking API'
            or 'post' not in api.get('paths', {}).get('/reservations', {})):
        raise ValueError('Unexpected API module or missing reservation route')
    request = urllib.request.Request(base + '/reservations/' + str(uuid4()),
                                     headers={'X-Customer-Id': 'lab-readonly-smoke'})
    try:
        with opener.open(request, timeout=10):
            raise ValueError('Unexpected success for a random nonexistent reservation')
    except urllib.error.HTTPError as error:
        with error:
            if error.code != 404 or json.load(error) != {'detail': {'code': 'RESERVATION_NOT_FOUND'}}:
                raise ValueError('Unexpected reservation lookup response')
    print(json.dumps({'result': 'BOOKING_HTTP_OK', 'health': 'alive', 'readiness': 'ready',
                      'api': 'trafficXX Booking API', 'missing_reservation_status': 404}))
except Exception as error:
    print(json.dumps({'result': 'HTTP_CHECK_FAILED', 'error_type': type(error).__name__,
                      'http_status': error.code if isinstance(error, urllib.error.HTTPError) else None}))
    raise SystemExit(1)
PY
