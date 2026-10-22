"""User-run healthy operations HTTP/metrics check through localhost port forward."""
import json
import math
import urllib.error
import urllib.request


def verify(get):
    if get('/healthz') != {'status': 'alive'} or get('/readyz') != {'status': 'ready'}:
        raise ValueError('Unexpected operations health/readiness')
    state = get('/backlog')
    values = state['metrics']
    if (state['native_queue_counts_available'] is not False or state['alerts'] != []
            or values['outbox_pending'] != 0 or values['processing_outstanding'] != 0
            or values['processed_total'] <= 0):
        raise ValueError('Expected a healthy database-derived backlog snapshot')
    history = ['DEAD_LETTERS_PREVIOUSLY_OBSERVED'] if values['observed_dead_letters_total'] else []
    if state['historical_observations'] != history:
        raise ValueError('Historical observations must be separate from active alerts')
    by_name = {worker['name']: worker for worker in state['workers']}
    for name in ('publisher', 'consumer'):
        worker = by_name[name]
        age = float(worker['age_seconds'])
        if (worker['present'] is not True or worker['has_error'] is not False
                or not math.isfinite(age) or not 0 <= age < 30):
            raise ValueError('Expected fresh error-free publisher/consumer heartbeats')
    required_rules = {'OUTBOX_BACKLOG_AGING', 'PROCESSING_BACKLOG_AGING'}
    required_rules |= {name + suffix for name in ('PUBLISHER', 'CONSUMER')
                       for suffix in ('_HEARTBEAT_MISSING', '_HEARTBEAT_STALE', '_ERROR')}
    if not required_rules <= set(state['alert_rules']) or any(state['alert_rules'].values()):
        raise ValueError('Expected complete healthy alert rule set')
    samples = {}
    for line in get('/metrics', text=True).splitlines():
        if line and not line.startswith('#'):
            key, value = line.rsplit(' ', 1)
            if key in samples:
                raise ValueError('Duplicate metric sample')
            samples[key] = float(value)
    for name in ('publisher', 'consumer'):
        label = '{worker="' + name + '"}'
        if (samples['trafficxx_worker_heartbeat_present' + label] != 1
                or samples['trafficxx_worker_has_error' + label] != 0
                or not 0 <= samples['trafficxx_worker_heartbeat_age_seconds' + label] < 30):
            raise ValueError('Unexpected worker metric sample')
    for name in required_rules:
        if samples['trafficxx_alert_active{alert="' + name + '"}'] != 0:
            raise ValueError('Unexpected active alert metric')
    if samples['trafficxx_outbox_pending'] != 0 or samples['trafficxx_processing_outstanding'] != 0:
        raise ValueError('Expected zero exported backlog')
    return {'result': 'OPERATIONS_HTTP_OK', 'health': 'alive', 'readiness': 'ready',
            'metrics': values, 'workers': [{k: by_name[name][k] for k in ('name', 'present', 'age_seconds', 'has_error')}
                                         for name in ('consumer', 'publisher')],
            'alerts': [], 'historical_observations': history, 'worker_and_alert_metrics_verified': True,
            'native_queue_counts_available': False}


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, text=False):
        with opener.open('http://127.0.0.1:18003' + path, timeout=15) as response:
            if text:
                if not response.headers.get('Content-Type', '').startswith('text/plain; version=0.0.4'):
                    raise ValueError('Unexpected metrics content type')
                return response.read().decode('utf-8')
            return json.load(response)
    try:
        print(json.dumps(verify(get)))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'OPERATIONS_HTTP_FAILED', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
