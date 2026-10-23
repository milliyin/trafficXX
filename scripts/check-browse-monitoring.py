"""Verify distinct Browse pod targets and real stored pool samples."""
import importlib.util
import ipaddress
import json
import math
from pathlib import Path
import time
import urllib.parse
import urllib.request

JOB = 'trafficxx-browse-pool'
FILTER = '{job="' + JOB + '"}'
QUERIES = {
    'up': 'up' + FILTER,
    'freshness_seconds': 'time() - timestamp(up' + FILTER + ')',
    'scrape_samples': 'count_over_time(up' + FILTER + '[2m])',
    **{suffix: 'trafficxx_browse_pool_' + suffix + FILTER for suffix in (
        'size', 'limit', 'available', 'waiting', 'borrows_total', 'borrow_errors_total',
        'connection_attempts_total', 'connection_errors_total', 'connections_lost_total', 'bad_returns_total')},
}


def vector(payload):
    if payload.get('status') != 'success' or payload['data']['resultType'] != 'vector':
        raise ValueError('Invalid Prometheus vector')
    result = {}
    for row in payload['data']['result']:
        key = row['metric']['instance']
        value = float(row['value'][1])
        if key in result or not math.isfinite(value):
            raise ValueError('Duplicate or nonfinite sample')
        result[key] = value
    return result


def verify(get, expected_instances=None):
    targets = [row for row in get('/api/v1/targets')['data']['activeTargets']
               if row['labels'].get('job') == JOB]
    if len(targets) != 2:
        raise ValueError('Expected two Browse targets')
    instances = set()
    for target in targets:
        instance = target['labels']['instance']
        host, port = instance.rsplit(':', 1)
        if (ipaddress.ip_address(host) not in ipaddress.ip_network('10.244.0.0/16')
                or port != '8000' or target['health'] != 'up' or target.get('lastError')
                or target['scrapeUrl'] != 'http://' + instance + '/metrics'):
            raise ValueError('Unexpected or unhealthy Browse target')
        instances.add(instance)
    if len(instances) != 2 or (expected_instances is not None and instances != set(expected_instances)):
        raise ValueError('Browse targets do not match serving pods')
    samples = {name: vector(get('/api/v1/query?' + urllib.parse.urlencode({'query': query})))
               for name, query in QUERIES.items()}
    if any(set(values) != instances for values in samples.values()):
        raise ValueError('Metric instances differ from current targets')
    per_instance = {}
    for instance in sorted(instances):
        values = {name: rows[instance] for name, rows in samples.items()}
        if (values['up'] != 1 or not 0 <= values['freshness_seconds'] <= 45
                or values['scrape_samples'] < 2 or values['limit'] != 4
                or not 1 <= values['size'] <= 4 or not 0 <= values['available'] <= values['size']
                or values['waiting'] != 0 or values['borrows_total'] < 1
                or values['connection_attempts_total'] < 1
                or any(value < 0 for value in values.values())):
            raise ValueError('Expected fresh healthy stored pool samples')
        per_instance[instance] = values
    return {'result': 'BROWSE_PROMETHEUS_OK', 'target_count': 2, 'per_instance': per_instance,
            'stored_samples_per_target_minimum': 2, 'metrics_borrow_sql_connections': False,
            'notification_delivery_configured': False}


def wait(get, expected_instances=None, sleep=time.sleep):
    for attempt in range(36):
        try:
            return verify(get, expected_instances)
        except (ValueError, KeyError, OSError):
            if attempt == 35:
                raise
            sleep(5)


def main():
    try:
        # User Cloud Shell only; correlate stored series to actual stable pods.
        spec = importlib.util.spec_from_file_location(
            'browse_concurrency', Path(__file__).with_name('check-browse-concurrency.py'))
        flow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(flow)
        deployment = json.loads(flow.kubectl('get', 'deployment', 'browse', '-n', 'trafficxx', '-o', 'json'))
        items = json.loads(flow.kubectl('get', 'pods', '-n', 'trafficxx', '-l',
                                      'app.kubernetes.io/name=trafficxx-browse', '-o', 'json'))['items']
        if len(items) != 2:
            raise ValueError('Wait for exactly two stable Browse pods')
        owner = next(row for row in items[0]['metadata']['ownerReferences'] if row.get('controller'))
        replica_set = json.loads(flow.kubectl('get', 'replicaset', owner['name'], '-n', 'trafficxx', '-o', 'json'))
        pods = flow.select_pods(deployment, items, replica_set)
        expected = {pod['status']['podIP'] + ':8000' for pod in pods}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path):
            with opener.open('http://127.0.0.1:18090' + path, timeout=15) as response:
                return json.load(response)

        print(json.dumps(wait(get, expected)))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_PROMETHEUS_FAILED', 'error_type': type(error).__name__,
                          'note': 'Inspect Browse metrics, headless DNS, Prometheus targets and rollout; no raw errors printed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
