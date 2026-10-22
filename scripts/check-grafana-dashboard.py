"""AKS-only read checks for provisioned dashboard/data source; no secret output."""
import base64
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'http://grafana.trafficxx-monitoring.svc.cluster.local:3000'
UID = 'trafficxx-operations'
SOURCE = 'trafficxx-prometheus'


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


def main():
    try:
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
            get('/api/dashboards/uid/' + UID, authenticated=False)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        else:
            raise ValueError('Dashboard unexpectedly accessible without login')
        for attempt in range(12):
            try:
                result = verify(get)
                result['unauthenticated_dashboard_denied'] = True
                print(json.dumps(result))
                return 0
            except (ValueError, KeyError, OSError):
                if attempt == 11:
                    raise
                time.sleep(5)
    except Exception as error:
        print(json.dumps({'result': 'GRAFANA_DASHBOARD_FAILED', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
