"""Cloud Shell: scale Booking 2->1->2 and prove native warning/recovery."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
MARKER = 'trafficxx.io/booking-alert-drill'
SELECTOR = 'app.kubernetes.io/name=trafficxx-booking'


class CheckFailed(ValueError):
    pass


class Retry(CheckFailed):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def command(args):
    return subprocess.check_output(args, text=True, timeout=30, stderr=subprocess.DEVNULL).strip()


def kube(*args):
    return command(['kubectl', '--request-timeout=20s', *args])


def deployment():
    return json.loads(kube('get', 'deployment', 'booking', '-n', 'trafficxx', '-o', 'json'))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch(resource, operations):
    checks = [{'op': 'test', 'path': '/metadata/uid', 'value': resource['metadata']['uid']},
              {'op': 'test', 'path': '/metadata/resourceVersion',
               'value': resource['metadata']['resourceVersion']}]
    return json.loads(kube('patch', 'deployment', 'booking', '-n', 'trafficxx',
                          '--type=json', '--patch', json.dumps(checks + operations), '-o', 'json'))


def scale_down(initial, run_id):
    annotations = dict(initial['metadata'].get('annotations', {}))
    require(MARKER not in annotations, 'EXISTING_DRILL_MARKER')
    annotations[MARKER] = run_id
    return patch(initial, [
        {'op': 'test', 'path': '/spec/replicas', 'value': 2},
        {'op': 'add', 'path': '/metadata/annotations', 'value': annotations},
        {'op': 'replace', 'path': '/spec/replicas', 'value': 1}])


def restore(initial, run_id):
    # Read/retry on resourceVersion conflicts; never overwrite another drill.
    for attempt in range(3):
        current = deployment()
        require(current['metadata']['uid'] == initial['metadata']['uid'], 'DEPLOYMENT_REPLACED')
        marker = current['metadata'].get('annotations', {}).get(MARKER)
        if marker is None:
            require(current['spec']['replicas'] == 2, 'RESTORATION_OWNERSHIP_UNKNOWN')
            return False
        require(marker == run_id, 'RESTORATION_MARKER_CHANGED')
        require(current['spec']['replicas'] in (1, 2), 'UNEXPECTED_REPLICA_CHANGE')
        try:
            patch(current, [{'op': 'test', 'path': '/metadata/annotations/trafficxx.io~1booking-alert-drill',
                             'value': run_id},
                            {'op': 'replace', 'path': '/spec/replicas', 'value': 2}])
            return True
        except (subprocess.SubprocessError, OSError):
            if attempt == 2:
                raise
            time.sleep(1)


def stable(initial, count):
    current = deployment()
    require(current['metadata']['uid'] == initial['metadata']['uid']
            and current['spec']['template'] == initial['spec']['template'], 'DEPLOYMENT_CHANGED')
    require(current['spec']['replicas'] == count, 'WRONG_DESIRED_REPLICAS')
    status = current.get('status', {})
    if (status.get('observedGeneration') != current['metadata']['generation']
            or any(status.get(key) != count for key in
                   ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas'))):
        raise Retry('WAITING_FOR_REPLICAS')
    sets = json.loads(kube('get', 'replicasets', '-n', 'trafficxx', '-l', SELECTOR, '-o', 'json'))['items']
    owned = {row['metadata']['uid'] for row in sets if any(
        owner.get('controller') and owner.get('kind') == 'Deployment'
        and owner['uid'] == initial['metadata']['uid']
        for owner in row['metadata'].get('ownerReferences', []))}
    pods = json.loads(kube('get', 'pods', '-n', 'trafficxx', '-l', SELECTOR, '-o', 'json'))['items']
    if len(pods) != count:
        raise Retry('WAITING_FOR_PODS')
    rows = []
    for pod in pods:
        require(any(owner.get('controller') and owner.get('kind') == 'ReplicaSet'
                    and owner['uid'] in owned for owner in pod['metadata'].get('ownerReferences', [])),
                'POD_OWNER_CHANGED')
        ready = not pod['metadata'].get('deletionTimestamp') and pod['status'].get('phase') == 'Running'
        container = next((row for row in pod['status'].get('containerStatuses', [])
                          if row['name'] == 'booking'), {})
        if not (ready and container.get('ready') is True
                and any(row['type'] == 'Ready' and row['status'] == 'True'
                        for row in pod['status'].get('conditions', []))):
            raise Retry('WAITING_FOR_READY_PODS')
        rows.append({'pod': pod['metadata']['name'], 'uid': pod['metadata']['uid'],
                     'restarts': container['restartCount']})
    return rows


def wait(check, seconds, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + seconds
    while True:
        try:
            return check()
        except (Retry, OSError, subprocess.SubprocessError):
            if clock() >= deadline:
                raise
            sleep(5)


def scalar(get, expression):
    payload = get('/api/v1/query?' + urllib.parse.urlencode({'query': expression}))
    rows = payload.get('data', {}).get('result', [])
    require(payload.get('status') == 'success' and payload['data'].get('resultType') == 'vector'
            and len(rows) == 1, 'EXPECTED_SINGLE_QUERY_VALUE')
    return float(rows[0]['value'][1])


def firing(get, alerts):
    name = 'TrafficxxBookingTargetsUnavailable'
    groups = [row for row in get('/api/v1/rules')['data']['groups']
              if row['name'] == 'trafficxx-booking-pool']
    require(len(groups) == 1 and groups[0]['interval'] == 15, 'RULE_GROUP_CHANGED')
    rules = groups[0]['rules']
    expected = alerts.rules_for('booking')
    require(len(rules) == 5 and {row['name'] for row in rules} == set(expected), 'RULES_CHANGED')
    for row in rules:
        duration, expression = expected[row['name']]
        require(row['type'] == 'alerting' and row['duration'] == duration
                and row['health'] == 'ok' and not row.get('lastError')
                and row['labels'].get('component') == 'booking-pool'
                and row['labels'].get('severity') == 'warning'
                and 0 <= time.time() - alerts.evaluation_epoch(row['lastEvaluation']) <= 45
                and alerts.canonical(get, row['query']) == alerts.canonical(get, expression),
                'RULE_METADATA_CHANGED')
        if row['name'] == name:
            if row['state'] != 'firing':
                raise Retry('WAITING_FOR_TARGET_WARNING')
        else:
            require(row['state'] == 'inactive', 'UNEXPECTED_ACTIVE_RULE')
    selector = '{component="booking-pool",job="trafficxx-booking-pool"'
    if scalar(get, '(sum(up{job="trafficxx-booking-pool"}) or vector(0))') != 1:
        raise Retry('EXPECTED_ONE_HEALTHY_SCRAPE')
    require(scalar(get, 'count(ALERTS' + selector + ',alertstate="firing",alertname="' + name + '"}) or vector(0)') == 1,
            'FIRING_SERIES_MISSING')
    require(scalar(get, 'count(ALERTS' + selector + ',alertstate="firing"}) or vector(0)') == 1,
            'UNEXPECTED_FIRING_ALERTS')
    return {'alert': name, 'state': 'firing', 'healthy_scrape_targets': 1,
            'hold_seconds': 120, 'native_firing_series_verified': True}


def history(get, started):
    expression = 'ALERTS{component="booking-pool",job="trafficxx-booking-pool",alertname="TrafficxxBookingTargetsUnavailable",alertstate="firing"}'
    payload = get('/api/v1/query_range?' + urllib.parse.urlencode(
        {'query': expression, 'start': started, 'end': time.time(), 'step': 15}))
    require(payload.get('status') == 'success' and payload['data'].get('resultType') == 'matrix',
            'FIRING_HISTORY_QUERY_FAILED')
    require(any(float(value) == 1 and float(timestamp) >= started
                for row in payload['data']['result'] for timestamp, value in row.get('values', [])),
            'STORED_FIRING_HISTORY_MISSING')


def run(get, observe_seconds=30):
    require(type(observe_seconds) is int and 0 <= observe_seconds <= 60, 'INVALID_OBSERVATION_WINDOW')
    alerts = load('check-browse-alerts')
    initial = deployment()
    require(initial['spec']['replicas'] == 2, 'EXPECTED_TWO_REPLICAS')
    require(MARKER not in initial['metadata'].get('annotations', {}), 'EXISTING_DRILL_MARKER')
    hpas = json.loads(kube('get', 'hpa', '-n', 'trafficxx', '-o', 'json'))['items']
    require(not any(row['spec']['scaleTargetRef'].get('kind') == 'Deployment'
                    and row['spec']['scaleTargetRef'].get('name') == 'booking' for row in hpas), 'BOOKING_HPA_PRESENT')
    before = stable(initial, 2)
    baseline = alerts.verify(get, component='booking')
    alerts.verify(get)  # Existing Browse rule health is also required.
    run_id = uuid4().hex[:12]
    started = time.time()
    observed, one = None, None
    restoration = {'desired_replicas_restored': False, 'ready_replicas_restored': False}
    try:
        print(json.dumps({'result': 'BOOKING_ALERT_DRILL_STARTED', 'run_id': run_id,
                          'deployment_uid': initial['metadata']['uid'],
                          'replicas': [2, 1, 2], 'hold_seconds': 120,
                          'note': 'Keep Cloud Shell running; no booking requests are generated.'}), flush=True)
        scale_down(initial, run_id)
        one = wait(lambda: stable(initial, 1), 180)
        require(one[0] in before, 'SURVIVING_PROCESS_CHANGED')

        def observe():
            require(stable(initial, 1) == one, 'SURVIVING_PROCESS_CHANGED')
            return firing(get, alerts)
        observed = wait(observe, 240)
        print(json.dumps({'result': 'BOOKING_TARGET_WARNING_OBSERVED', **observed,
                          'run_id': run_id, 'observation_seconds': observe_seconds}), flush=True)
        for elapsed in range(0, observe_seconds, 5):
            time.sleep(min(5, observe_seconds - elapsed))
            observe()
    finally:
        # Includes a lost patch response or Ctrl+C. Marker prevents stealing a
        # concurrent drill's restoration. Hard session termination cannot run finally.
        try:
            restore(initial, run_id)
            restoration['desired_replicas_restored'] = True
            restored = wait(lambda: stable(initial, 2), 180)
            restoration['ready_replicas_restored'] = True
            current = deployment()
            if current['metadata'].get('annotations', {}).get(MARKER) == run_id:
                patch(current, [{'op': 'remove', 'path': '/metadata/annotations/trafficxx.io~1booking-alert-drill'}])
            print(json.dumps({'result': 'BOOKING_REPLICAS_RESTORED', **restoration}), flush=True)
        except BaseException:
            print(json.dumps({'result': 'BOOKING_RESTORATION_INCOMPLETE', **restoration,
                              'note': 'Inspect Booking desired/ready replicas and drill annotation before further experiments.'}), flush=True)
            raise
    require(one is not None and observed is not None and one[0] in restored, 'RECOVERY_EVIDENCE_INCOMPLETE')
    def healthy():
        try:
            return alerts.verify(get, component='booking')
        except ValueError:
            raise Retry('WAITING_FOR_HEALTHY_RULES_AND_STORED_SAMPLES') from None
    recovered = wait(healthy, 180)
    alerts.verify(get)
    history(get, started)
    return {'result': 'BOOKING_ALERT_RECOVERY_OK', 'run_id': run_id,
            'before': {'targets': baseline['target_count'], 'healthy_inactive_rules': baseline['rule_count']},
            'during': observed, 'after': {'targets': recovered['target_count'], 'healthy_inactive_rules': recovered['rule_count']},
            'ready_replicas': 2, 'surviving_pod': one[0]['pod'], 'stored_firing_history_verified': True,
            'inventory_writes': False, 'booking_http_continuity_tested': False,
            'notification_delivery_configured': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-seconds', type=int, default=30)
    args = parser.parse_args()
    try:
        require(command(['az', 'account', 'show', '--query', 'id', '-o', 'tsv'])
                == '35aaf788-f4cf-40c3-9ea5-bc8fe8708a85', 'WRONG_SUBSCRIPTION')
        require(kube('config', 'current-context') == 'trafficxx-lab-aks', 'WRONG_CONTEXT')
        with tempfile.TemporaryFile(mode='w+') as log:
            forward = subprocess.Popen(['kubectl', 'port-forward', '-n', 'trafficxx-monitoring',
                                        '--address=127.0.0.1', 'service/prometheus', '18091:9090'],
                                       stdout=log, stderr=log)
            try:
                for attempt in range(80):
                    require(forward.poll() is None, 'PORT_FORWARD_EXITED')
                    log.seek(0)
                    if 'Forwarding from 127.0.0.1:18091' in log.read():
                        break
                    time.sleep(0.25)
                else:
                    raise CheckFailed('PORT_FORWARD_NOT_READY')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def get(path):
                    with opener.open('http://127.0.0.1:18091' + path, timeout=10) as response:
                        return json.load(response)
                print(json.dumps(run(get, args.observe_seconds)), flush=True)
            finally:
                forward.terminate()
                forward.wait(timeout=10)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'result': 'BOOKING_ALERT_RECOVERY_FAILED', 'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, CheckFailed) else None,
                          'recovery': 'Check deployment/booking desired and ready replicas; expected 2. Do not rerun the drill to diagnose.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
