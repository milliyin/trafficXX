"""Database-derived monitoring rules and exposition, independent of transport."""
import json
import math
import re


def expected_workers(value):
    names = sorted({name.strip() for name in value.split(',') if name.strip()})
    if any(not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) for name in names):
        raise ValueError('Invalid expected heartbeat worker name')
    return names


def evaluate(values, rows, threshold, expected=()):
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('BACKLOG_ALERT_SECONDS must be positive and finite')
    rules = {
        'OUTBOX_BACKLOG_AGING': bool(values['outbox_pending'] and values['outbox_oldest_seconds'] >= threshold),
        'PROCESSING_BACKLOG_AGING': bool(values['processing_outstanding'] and values['processing_oldest_seconds'] >= threshold),
    }
    recorded = {row['name']: row for row in rows}
    workers = []
    for name in sorted(set(recorded) | set(expected)):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
            raise ValueError('Invalid recorded heartbeat worker name')
        row = recorded.get(name)
        present = row is not None
        age = float(row['age_seconds']) if present else None
        if age is not None and (not math.isfinite(age) or age < 0):
            raise ValueError('Invalid heartbeat age')
        has_error = bool(row['last_error']) if present else False
        rules[name.upper() + '_HEARTBEAT_MISSING'] = not present
        rules[name.upper() + '_HEARTBEAT_STALE'] = present and age >= threshold
        rules[name.upper() + '_ERROR'] = has_error
        workers.append({'name': name, 'present': present, 'age_seconds': age,
                        'last_seen_at': row['last_seen_at'] if present else None, 'has_error': has_error})
    return {'metrics': values, 'workers': workers,
            'alerts': sorted(name for name, active in rules.items() if active),
            'alert_rules': rules,
            'historical_observations': ['DEAD_LETTERS_PREVIOUSLY_OBSERVED']
                if values['observed_dead_letters_total'] else [],
            'native_queue_counts_available': False}


def render_metrics(state):
    lines = []
    for name, value in state['metrics'].items():
        kind = 'counter' if name.endswith('_total') else 'gauge'
        lines.extend([f'# TYPE trafficxx_{name} {kind}', f'trafficxx_{name} {value}'])
    for metric in ('worker_heartbeat_present', 'worker_heartbeat_age_seconds', 'worker_has_error', 'alert_active'):
        lines.append(f'# TYPE trafficxx_{metric} gauge')
    for worker in state['workers']:
        label = '{worker=' + json.dumps(worker['name']) + '}'
        lines.append(f'trafficxx_worker_heartbeat_present{label} {int(worker["present"])}')
        # Missing workers have no age; presence=0 and MISSING alert carry that fact.
        if worker['present']:
            lines.append(f'trafficxx_worker_heartbeat_age_seconds{label} {worker["age_seconds"]}')
        lines.append(f'trafficxx_worker_has_error{label} {int(worker["has_error"])}')
    for name, active in sorted(state['alert_rules'].items()):
        label = '{alert=' + json.dumps(name) + '}'
        lines.append(f'trafficxx_alert_active{label} {int(active)}')
    return '\n'.join(lines) + '\n'
