"""External, credential-free bounded functional probes and actual check history."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(base):
    results = []
    opener = urllib.request.build_opener(NoRedirect())
    for name, path in [('Website', '/'), ('Click API readiness', '/readyz'), ('Leaderboard', '/api/leaderboard')]:
        start = datetime.now(timezone.utc)
        ok = False
        try:
            with opener.open(urllib.request.Request(base + path, headers={'User-Agent': 'ClickClash-External-Status/1'}), timeout=10) as response:
                body = response.read(262145)
                if len(body) > 262144 or response.status != 200:
                    raise ValueError
                if path == '/':
                    ok = b'ClickClash' in body
                else:
                    value = json.loads(body)
                    ok = value.get('readiness') == 'ready' if path == '/readyz' else isinstance(value.get('entries'), list) and isinstance(value.get('totals'), dict)
        except (OSError, ValueError):
            pass  # Public status must not disclose response bodies or infrastructure errors.
        results.append({'name': name, 'ok': ok, 'latency_ms': round((datetime.now(timezone.utc) - start).total_seconds() * 1000, 1)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, default=Path('docs/public-status/history.json'))
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        parser.error('Provide the operator-reviewed public HTTPS origin, with no credentials/path/query')
    # The URL is operator configuration, never accepted from a public API/player.
    base = args.url.rstrip('/')
    current = probe(base)
    stamp = datetime.now(timezone.utc).isoformat()
    old = json.loads(args.output.read_text()) if args.output.exists() else {}
    if old.get('origin') != base:
        old = {}  # Never mix different deployment histories.
    history = old.get('history', [])[-287:]
    history.append({'checked_at': stamp, 'ok': all(item['ok'] for item in current),
                    'components': current})
    value = {'origin': base, 'started_at': old.get('started_at', stamp), 'checked_at': stamp,
             'components': current, 'history': history, 'note': 'Actual functional probe results; missing time is not assumed healthy. No authenticated login or click-write availability claim.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps({'result': 'CLICKCLASH_STATUS_RECORDED', 'components': current}))


if __name__ == '__main__':
    main()
