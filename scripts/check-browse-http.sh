#!/usr/bin/env bash
# User-run check through a temporary localhost-only Kubernetes port forward.
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "${1:-}" != --demo ) ]]; then
  echo 'Usage: bash check-browse-http.sh [--demo]' >&2; exit 2
fi
[[ "$(kubectl config current-context)" == trafficxx-lab-aks ]] || {
  echo 'Select the trafficxx-lab-aks context first.' >&2; exit 1;
}
forward_log=$(mktemp "${TMPDIR:-/tmp}/trafficxx-browse-forward.XXXXXX")
kubectl port-forward -n trafficxx --address=127.0.0.1 service/browse 18001:80 > "$forward_log" 2>&1 &
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
  if grep -q 'Forwarding from 127.0.0.1:18001' "$forward_log"; then break; fi
  sleep 0.25
done
grep -q 'Forwarding from 127.0.0.1:18001' "$forward_log" || {
  echo 'Port forward did not become ready.' >&2; exit 1;
}
python3 - "${1:-}" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

base = 'http://127.0.0.1:18001'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path, timeout=10):
    with opener.open(base + path, timeout=timeout) as response:
        return json.load(response)

for attempt in range(30):
    try:
        health = get('/healthz', timeout=1)
        if health != {'status': 'alive'}:
            raise ValueError('Unexpected health response')
        break
    except (OSError, ValueError):
        if attempt == 29:
            raise SystemExit('HTTP_CHECK_FAILED: health endpoint unavailable')
        time.sleep(0.25)
try:
    ready = get('/readyz')
    events = get('/events')
    if ready != {'status': 'ready'} or not isinstance(events, list):
        raise ValueError('Unexpected API response')
    result = {'result': 'BROWSE_HTTP_OK', 'health': 'alive',
              'readiness': 'ready', 'event_count': len(events)}
    if sys.argv[1] == '--demo':
        by_id = {event['id']: event for event in events}
        expected = {'demo-general': 'GENERAL', 'demo-seated': 'SEATED'}
        availability = {}
        for event_id, inventory_type in expected.items():
            if by_id[event_id]['inventory_type'] != inventory_type:
                raise ValueError('Unexpected demo event type')
            data = get('/events/' + event_id + '/availability')
            available = data['available']
            if (data['id'] != event_id or data['inventory_type'] != inventory_type
                    or type(available) is not int or not 0 <= available <= 100):
                raise ValueError('Unexpected availability response')
            if inventory_type == 'GENERAL':
                if data['seats'] is not None:
                    raise ValueError('General inventory must not have labelled seats')
            else:
                seats = data['seats']
                if (not isinstance(seats, list) or not all(isinstance(seat, str) for seat in seats)
                        or len(seats) != available or len(set(seats)) != available
                        or not set(seats) <= {f'A{n}' for n in range(1, 101)}):
                    raise ValueError('Unexpected assigned-seat labels')
            availability[event_id] = available
        result.update({'result': 'BROWSE_DEMO_HTTP_OK', 'demo_availability': availability})
    print(json.dumps(result))
except Exception as error:
    print(json.dumps({'result': 'HTTP_CHECK_FAILED', 'error_type': type(error).__name__,
                      'http_status': error.code if isinstance(error, urllib.error.HTTPError) else None}))
    raise SystemExit(1)
PY
