"""Read-only proof of installed rules, fresh evaluation and healthy Browse samples."""
from datetime import datetime
import importlib.util
import json
import re
from pathlib import Path
import time
import urllib.parse
import urllib.request

JOB = 'trafficxx-browse-pool'
RULES = {
    'TrafficxxBrowseTargetsUnavailable': (120, '(sum(up{job="trafficxx-browse-pool"}) or vector(0)) < 2'),
    'TrafficxxBrowsePoolMetricsMissing': (120, '(up{job="trafficxx-browse-pool"} == 1) unless on(job, instance) trafficxx_browse_pool_size{job="trafficxx-browse-pool"}'),
    'TrafficxxBrowsePoolWaiting': (60, 'trafficxx_browse_pool_waiting{job="trafficxx-browse-pool"} > 0'),
    'TrafficxxBrowsePoolBorrowErrors': (60, 'increase(trafficxx_browse_pool_borrow_errors_total{job="trafficxx-browse-pool"}[5m]) > 0'),
    'TrafficxxBrowsePoolConnectionErrors': (60, 'increase(trafficxx_browse_pool_connection_errors_total{job="trafficxx-browse-pool"}[5m]) > 0'),
}


class CheckFailed(ValueError):
    pass


def require(condition, check):
    if not condition:
        raise CheckFailed(check)


def canonical(get, expression):
    payload = get('/api/v1/format_query?' + urllib.parse.urlencode({'query': expression}))
    require(payload.get('status') == 'success' and isinstance(payload.get('data'), str)
            and payload['data'].strip(), 'EXPRESSION_FORMAT_FAILED')
    return ''.join(payload['data'].split())


def evaluation_epoch(value):
    # Prometheus emits RFC3339Nano; Python3.10 only accepts microseconds.
    value = re.sub(r'(\.\d{6})\d+(?=[+-]|$)', r'\1', value.replace('Z', '+00:00'))
    evaluated = datetime.fromisoformat(value)
    require(evaluated.tzinfo is not None, 'RULE_EVALUATION_TIMEZONE_MISSING')
    return evaluated.timestamp()


def verify(get, clock=time.time):
    payload = get('/api/v1/rules')
    require(payload.get('status') == 'success', 'RULES_API_FAILED')
    groups = [row for row in payload['data']['groups'] if row['name'] == JOB]
    require(len(groups) == 1 and groups[0]['interval'] == 15, 'RULE_GROUP_MISSING_OR_WRONG_INTERVAL')
    rules = groups[0]['rules']
    require(len(rules) == 5 and {row['name'] for row in rules} == set(RULES), 'EXPECTED_FIVE_RULES')
    states = {}
    for rule in rules:
        name = rule['name']
        duration, expression = RULES[name]
        require(rule['type'] == 'alerting' and rule['duration'] == duration, 'RULE_TYPE_OR_HOLD_CHANGED')
        require(rule['labels'].get('component') == 'browse-pool'
                and rule['labels'].get('severity') == 'warning', 'RULE_LABELS_CHANGED')
        require(rule['health'] == 'ok' and not rule.get('lastError'), 'RULE_EVALUATION_ERROR')
        require(0 <= clock() - evaluation_epoch(rule['lastEvaluation']) <= 45,
                'RULE_EVALUATION_NOT_FRESH')
        require(canonical(get, rule['query']) == canonical(get, expression), 'INSTALLED_EXPRESSION_CHANGED')
        require(rule['state'] == 'inactive' and not rule.get('alerts'), 'BROWSE_RULE_ACTIVE_' + name)
        states[name] = {'state': rule['state'], 'health': rule['health'], 'hold_seconds': duration}
    spec = importlib.util.spec_from_file_location(
        'browse_monitoring', Path(__file__).with_name('check-browse-monitoring.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    samples = module.verify(get)
    return {'result': 'BROWSE_ALERT_RULES_OK', 'rules': states, 'rule_count': 5,
            'target_count': samples['target_count'], 'fresh_stored_samples_verified': True,
            'notification_delivery_configured': False,
            'note': 'Loaded healthy rules; synthetic firing/recovery tested separately. No email/phone notification tested.'}


def wait(get, sleep=time.sleep):
    for attempt in range(30):
        try:
            return verify(get)
        except (ValueError, KeyError, OSError):
            if attempt == 29:
                raise
            sleep(5)


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with opener.open('http://127.0.0.1:18090' + path, timeout=15) as response:
            return json.load(response)
    try:
        print(json.dumps(wait(get)))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'BROWSE_ALERT_RULES_FAILED', 'error_type': type(error).__name__,
                          'check': str(error) if isinstance(error, CheckFailed) else None,
                          'note': 'Inspect Prometheus config/rules and Browse metrics. Renewal Job was not modified.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
