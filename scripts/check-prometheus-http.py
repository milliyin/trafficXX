"""Verify real stored operations samples via the private Prometheus query API."""
import json
import math
import time
import urllib.parse
import urllib.request

JOB = 'trafficxx-operations'
QUERIES = {
    'up': 'up{job="trafficxx-operations"}',
    'freshness_seconds': 'time() - timestamp(up{job="trafficxx-operations"})',
    'samples_in_two_minutes': 'count_over_time(up{job="trafficxx-operations"}[2m])',
    'processed_total': 'trafficxx_processed_total{job="trafficxx-operations"}',
    'processing_outstanding': 'trafficxx_processing_outstanding{job="trafficxx-operations"}',
    'outbox_pending': 'trafficxx_outbox_pending{job="trafficxx-operations"}',
    'active_alerts': 'sum(trafficxx_alert_active{job="trafficxx-operations"})',
    'historical_dead_letters': 'trafficxx_observed_dead_letters_total{job="trafficxx-operations"}',
}


def scalar(payload):
    if payload.get('status') != 'success' or payload['data']['resultType'] != 'vector':
        raise ValueError('Unexpected query response')
    rows = payload['data']['result']
    if len(rows) != 1:
        raise ValueError('Expected exactly one target series')
    value = float(rows[0]['value'][1])
    if not math.isfinite(value):
        raise ValueError('Invalid numeric query sample')
    return value


def verify(get):
    get('/-/healthy', text=True)
    get('/-/ready', text=True)
    targets = get('/api/v1/targets')['data']['activeTargets']
    matches = [t for t in targets if t['labels'].get('job') == JOB]
    if (len(matches) != 1 or matches[0]['health'] != 'up' or matches[0].get('lastError')
            or matches[0]['scrapeUrl'] != 'http://operations.trafficxx.svc.cluster.local:80/metrics'):
        raise ValueError('Operations scrape target is not healthy')
    samples = {name: scalar(get('/api/v1/query?' + urllib.parse.urlencode({'query': query})))
               for name, query in QUERIES.items()}
    if (samples['up'] != 1 or not 0 <= samples['freshness_seconds'] <= 45
            or samples['samples_in_two_minutes'] < 2 or samples['processed_total'] <= 0
            or any(samples[k] != 0 for k in ('processing_outstanding', 'outbox_pending', 'active_alerts'))
            or samples['historical_dead_letters'] < 0):
        raise ValueError('Expected fresh stored samples for a healthy operations baseline')
    return {'result': 'PROMETHEUS_SCRAPE_OK', 'job': JOB, 'target_health': 'up',
            'samples': samples, 'storage_observation': 'at least two real scrape samples in the last two minutes',
            'native_queue_counts_available': False, 'notification_delivery_configured': False}


def wait(get, sleep=time.sleep):
    for attempt in range(24):
        try:
            return verify(get)
        except (ValueError, KeyError, OSError):
            if attempt == 23:
                raise
            sleep(5)


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, text=False):
        with opener.open('http://127.0.0.1:18090' + path, timeout=15) as response:
            return response.read().decode('utf-8') if text else json.load(response)
    try:
        print(json.dumps(wait(get)))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'PROMETHEUS_SCRAPE_FAILED', 'error_type': type(error).__name__,
                          'note': 'Inspect collector pods/PVC, config validation and target health; no raw scrape errors printed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
