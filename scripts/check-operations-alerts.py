"""User-run monitoring appearance/recovery drill; consumer restored by helper."""
import json
import math
from pathlib import Path
import runpy
import sys
import time
import urllib.request


def active_signals(get):
    state = get('/backlog')
    required = {'PROCESSING_BACKLOG_AGING', 'CONSUMER_HEARTBEAT_STALE'}
    if (set(state['alerts']) != required or state['native_queue_counts_available'] is not False
            or state['metrics']['processing_outstanding'] != 2
            or state['metrics']['processing_oldest_seconds'] < 30
            or state['metrics']['outbox_pending'] != 0):
        return None
    workers = {w['name']: w for w in state['workers']}
    consumer_age = float(workers['consumer']['age_seconds'])
    publisher_age = float(workers['publisher']['age_seconds'])
    if (not math.isfinite(consumer_age) or not math.isfinite(publisher_age)
            or workers['consumer']['present'] is not True or consumer_age < 30
            or workers['publisher']['present'] is not True or workers['publisher']['has_error']
            or not 0 <= publisher_age < 30):
        return None
    # The same state must be visible to a metrics scraper, not only the JSON API.
    samples = {}
    for line in get('/metrics', text=True).splitlines():
        if line and not line.startswith('#'):
            key, value = line.rsplit(' ', 1)
            if key in samples:
                return None
            samples[key] = float(value)
    if not all(math.isfinite(value) for value in samples.values()):
        return None
    if any(samples.get('trafficxx_alert_active{alert="' + name + '"}') != 1 for name in required):
        return None
    if (samples.get('trafficxx_processing_outstanding') != 2
            or samples.get('trafficxx_worker_heartbeat_age_seconds{worker="consumer"}', -1) < 30
            or samples.get('trafficxx_alert_active{alert="PUBLISHER_HEARTBEAT_STALE"}') != 0):
        return None
    return {'alerts': sorted(state['alerts']), 'metrics': state['metrics'],
            'workers': [{k: workers[name][k] for k in ('name', 'present', 'age_seconds', 'has_error')}
                        for name in ('consumer', 'publisher')],
            'historical_observations': state['historical_observations'],
            'active_alert_metrics_verified': True}


def wait_for_alerts(get, sleep=time.sleep):
    for _ in range(18):
        state = active_signals(get)
        if state is not None:
            print(json.dumps({'result': 'OPERATIONS_ALERTS_OBSERVED', **state}), flush=True)
            return state
        sleep(5)
    raise TimeoutError('Aged backlog and stale consumer signals were not both observed')


def run(get, drill, healthy_check, sleep=time.sleep):
    baseline = healthy_check(get)
    observed = []

    def paused():
        observed.append(wait_for_alerts(get, sleep))
    # The existing drill's finally block restores the consumer if this callback fails.
    recovery = drill(while_paused=paused)
    if recovery.get('result') != 'CONSUMER_BACKLOG_RECOVERY_OK' or len(observed) != 1:
        raise ValueError('Backlog and recovery evidence incomplete')
    for _ in range(12):
        try:
            after = healthy_check(get)
            return {'result': 'OPERATIONS_ALERT_RECOVERY_OK', 'before': baseline,
                    'while_paused': observed[0], 'after': after, 'backlog_recovery': recovery}
        except (ValueError, KeyError, OSError):
            sleep(5)
    raise TimeoutError('Healthy operations snapshot/zero alert gauges did not return')


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, text=False):
        with opener.open('http://127.0.0.1:18003' + path, timeout=15) as response:
            return response.read().decode('utf-8') if text else json.load(response)
    try:
        folder = Path(__file__).resolve().parent
        import os
        os.chdir(folder.parent)
        backlog = runpy.run_path(str(folder / 'check-consumer-backlog.py'))
        http = runpy.run_path(str(folder / 'check-operations-http.py'))
        print(json.dumps(run(get, backlog['run'], http['verify'])))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'result': 'OPERATIONS_ALERT_RECOVERY_FAILED', 'error_type': type(error).__name__,
                          'recovery': 'Inspect deployment/consumer replicas and readiness; restore one replica if needed. No external notification delivery was tested.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
