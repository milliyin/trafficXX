"""AKS-only read checks for provisioned dashboard/data source; no secret output."""
import base64
import json
import importlib.util
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://grafana.trafficxx-monitoring.svc.cluster.local:3000'
UID = 'trafficxx-operations'
SOURCE = 'trafficxx-prometheus'
POOL_UID = 'trafficxx-browse-pool'
BOOKING_POOL_UID = 'trafficxx-booking-pool'
ALERT_FILTER = 'component="browse-pool",job="trafficxx-browse-pool"'
ALERT_QUERIES = {
    state: 'count(ALERTS{' + ALERT_FILTER + ',alertstate="' + state + '"}) or vector(0)'
    for state in ('pending', 'firing')
}
ALERT_HISTORY = 'ALERTS{' + ALERT_FILTER + '}'


def verify(get):
    health = get('/api/health')
    if health.get('database') != 'ok':
        raise ValueError('Grafana database is not healthy')
    source = get('/api/datasources/uid/' + SOURCE)
    if (source.get('uid') != SOURCE or source.get('type') != 'prometheus'
            or source.get('url') != 'http://prometheus.trafficxx-monitoring.svc.cluster.local:9090'):
        raise ValueError('Unexpected provisioned data source')
    source_health = get('/api/datasources/uid/' + SOURCE + '/health')
    if source_health.get('status') != 'OK':
        raise ValueError('Prometheus data source health failed')
    payload = get('/api/dashboards/uid/' + UID)
    dashboard = payload['dashboard']
    panels = dashboard.get('panels', [])
    if (dashboard.get('uid') != UID or dashboard.get('title') != 'trafficXX | Operations'
            or payload.get('meta', {}).get('provisioned') is not True or len(panels) != 12):
        raise ValueError('Expected the twelve-panel provisioned dashboard')
    for panel in panels:
        if panel.get('datasource', {}).get('uid') != SOURCE:
            raise ValueError('Unexpected dashboard data source reference')
    path = '/api/datasources/proxy/uid/' + SOURCE + '/api/v1/query?'
    query = get(path + urllib.parse.urlencode({'query': 'up{job="trafficxx-operations"}'}))
    rows = query.get('data', {}).get('result', [])
    if (query.get('status') != 'success' or query['data'].get('resultType') != 'vector'
            or len(rows) != 1 or float(rows[0]['value'][1]) != 1):
        raise ValueError('Actual data source query did not find the healthy operations target')
    return {'result': 'GRAFANA_DASHBOARD_OK', 'grafana_version': health.get('version'),
            'database': 'ok', 'dashboard_uid': UID, 'dashboard_title': dashboard['title'],
            'panel_count': len(panels), 'datasource_uid': SOURCE, 'datasource_health': 'OK',
            'operations_up_via_datasource': 1, 'notification_delivery_configured': False}


def verify_pool(get):
    verify(get)  # Preserve proof that the existing Operations dashboard still works.
    payload = get('/api/dashboards/uid/' + POOL_UID)
    document = payload['dashboard']
    panels = document.get('panels', [])
    if (document.get('uid') != POOL_UID or document.get('title') != 'trafficXX | Browse database pool'
            or payload.get('meta', {}).get('provisioned') is not True or len(panels) != 10
            or any(panel.get('datasource', {}).get('uid') != SOURCE for panel in panels)):
        raise ValueError('Unexpected Browse pool dashboard')
    spec = importlib.util.spec_from_file_location(
        'browse_monitoring', Path(__file__).with_name('check-browse-monitoring.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = '/api/datasources/proxy/uid/' + SOURCE
    stored = module.verify(lambda path: get(prefix + path))
    by_id = {panel.get('id'): panel for panel in panels}
    if (len(by_id) != 10 or by_id.get(9, {}).get('type') != 'stat'
            or by_id.get(10, {}).get('type') != 'timeseries'
            or [row.get('expr') for row in by_id[9].get('targets', [])] != list(ALERT_QUERIES.values())
            or [row.get('expr') for row in by_id[10].get('targets', [])] != [ALERT_HISTORY]):
        raise ValueError('Expected pending/firing Browse alert panels')
    # Zero ALERTS alone cannot prove that any rules are loaded or evaluating.
    spec = importlib.util.spec_from_file_location(
        'browse_alerts', Path(__file__).with_name('check-browse-alerts.py'))
    alerts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alerts)
    rule_state = alerts.verify(lambda path: get(prefix + path))
    counts = {}
    for state, expression in ALERT_QUERIES.items():
        payload = get(prefix + '/api/v1/query?' + urllib.parse.urlencode({'query': expression}))
        rows = payload.get('data', {}).get('result', [])
        if (payload.get('status') != 'success' or payload['data'].get('resultType') != 'vector'
                or len(rows) != 1 or float(rows[0]['value'][1]) != 0):
            raise ValueError('Expected inactive Browse alert baseline through datasource')
        counts[state] = 0
    return {'result': 'GRAFANA_BROWSE_POOL_OK', 'dashboard_uid': POOL_UID,
            'panel_count': 10, 'datasource_uid': SOURCE, 'target_count': stored['target_count'],
            'alert_counts': counts, 'healthy_inactive_rule_count': rule_state['rule_count'],
            'stored_samples_per_target_minimum': 2,
            'operations_dashboard_verified': True, 'notification_delivery_configured': False}


def verify_booking_pool(get):
    verify_pool(get)  # Preserve Operations, Browse samples and Browse rule health.
    payload = get('/api/dashboards/uid/' + BOOKING_POOL_UID)
    dashboard = payload['dashboard']
    panels = dashboard.get('panels', [])
    if (dashboard.get('uid') != BOOKING_POOL_UID
            or dashboard.get('title') != 'trafficXX | Booking database pool'
            or payload.get('meta', {}).get('provisioned') is not True
            or len(panels) != 8 or len({row.get('id') for row in panels}) != 8):
        raise ValueError('Unexpected Booking pool dashboard')
    for panel in panels:
        if panel.get('datasource', {}).get('uid') != SOURCE or not panel.get('targets'):
            raise ValueError('Missing Booking dashboard datasource/queries')
        for target in panel['targets']:
            if (target.get('datasource', {}).get('uid') != SOURCE
                    or 'job="trafficxx-booking-pool"' not in target.get('expr', '')
                    or 'browse' in target['expr']):
                raise ValueError('Booking panel references unexpected metrics')
    spec = importlib.util.spec_from_file_location(
        'pool_monitoring', Path(__file__).with_name('check-browse-monitoring.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = '/api/datasources/proxy/uid/' + SOURCE
    stored = module.verify(lambda path: get(prefix + path), component='booking')
    return {'result': 'GRAFANA_BOOKING_POOL_OK', 'dashboard_uid': BOOKING_POOL_UID,
            'panel_count': 8, 'datasource_uid': SOURCE, 'target_count': stored['target_count'],
            'stored_samples_per_target_minimum': 2,
            'operations_dashboard_verified': True, 'browse_dashboard_and_rules_verified': True,
            'notification_delivery_configured': False}


def main():
    try:
        if sys.argv[1:] not in ([], ['--browse-pool'], ['--booking-pool']):
            raise ValueError('Unsupported dashboard check option')
        pool_mode = bool(sys.argv[1:])
        booking_mode = sys.argv[1:] == ['--booking-pool']
        user = Path('/credentials/admin-user').read_text().strip()
        password = Path('/credentials/admin-password').read_text().strip()
        if user != 'admin' or len(password) < 32:
            raise ValueError('Unexpected bootstrap credentials')
        auth = 'Basic ' + base64.b64encode((user + ':' + password).encode()).decode()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path, authenticated=True):
            request = urllib.request.Request(BASE + path, headers={'Authorization': auth} if authenticated else {})
            with opener.open(request, timeout=15) as response:
                return json.load(response)
        try:
            target_uid = BOOKING_POOL_UID if booking_mode else POOL_UID if pool_mode else UID
            get('/api/dashboards/uid/' + target_uid, authenticated=False)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        else:
            raise ValueError('Dashboard unexpectedly accessible without login')
        for attempt in range(48 if pool_mode else 12):
            try:
                result = (verify_booking_pool(get) if booking_mode else
                          verify_pool(get) if pool_mode else verify(get))
                result['unauthenticated_dashboard_denied'] = True
                print(json.dumps(result))
                return 0
            except (ValueError, KeyError, OSError):
                if attempt == (47 if pool_mode else 11):
                    raise
                time.sleep(5)
    except Exception as error:
        print(json.dumps({'result': 'GRAFANA_DASHBOARD_FAILED', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
