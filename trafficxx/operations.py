import os

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from trafficxx.db import connect
from trafficxx.health import register

app = FastAPI(title='trafficXX Local Operations', version='0.2.0')
register(app)


def snapshot():
    with connect() as conn:
        values = conn.execute(
            'SELECT count(*) FILTER (WHERE o.published_at IS NULL) AS outbox_pending, '
            'coalesce(greatest(0, extract(epoch FROM clock_timestamp() - '
            'min(o.created_at) FILTER (WHERE o.published_at IS NULL))), 0) AS outbox_oldest_seconds, '
            'count(*) FILTER (WHERE o.published_at IS NOT NULL AND i.message_id IS NULL) AS processing_outstanding, '
            'coalesce(greatest(0, extract(epoch FROM clock_timestamp() - min(o.created_at) '
            'FILTER (WHERE o.published_at IS NOT NULL AND i.message_id IS NULL))), 0) AS processing_oldest_seconds, '
            'coalesce(sum(o.publish_failures), 0) AS publish_failures_total '
            'FROM outbox o LEFT JOIN consumer_inbox i ON i.message_id = o.message_id'
        ).fetchone()
        values.update(conn.execute(
            'SELECT (SELECT count(*) FROM consumer_inbox) AS processed_total, '
            '(SELECT count(*) FROM worker_failures) AS processing_failures_total, '
            '(SELECT count(*) FROM observed_dead_letters) AS observed_dead_letters_total'
        ).fetchone())
        workers = conn.execute('SELECT name, last_seen_at, last_error, '
                               'greatest(0, extract(epoch FROM clock_timestamp() - last_seen_at)) AS age_seconds '
                               'FROM worker_heartbeats ORDER BY name').fetchall()
    threshold = float(os.environ.get('BACKLOG_ALERT_SECONDS', '30'))
    alerts = []
    if values['outbox_pending'] and values['outbox_oldest_seconds'] >= threshold:
        alerts.append('OUTBOX_BACKLOG_AGING')
    if values['processing_outstanding'] and values['processing_oldest_seconds'] >= threshold:
        alerts.append('PROCESSING_BACKLOG_AGING')
    if values['observed_dead_letters_total']:
        alerts.append('DEAD_LETTERS_OBSERVED')
    for worker in workers:
        if worker['age_seconds'] >= threshold:
            alerts.append(worker['name'].upper() + '_HEARTBEAT_STALE')
        if worker['last_error']:
            alerts.append(worker['name'].upper() + '_ERROR')
    return {'metrics': values, 'workers': workers, 'alerts': alerts,
            'native_queue_counts_available': False}


@app.get('/backlog')
def backlog():
    return snapshot()


@app.get('/metrics', response_class=PlainTextResponse)
def metrics():
    values = snapshot()['metrics']
    lines = []
    for name, value in values.items():
        kind = 'counter' if name.endswith('_total') else 'gauge'
        lines.extend([f'# TYPE trafficxx_{name} {kind}', f'trafficxx_{name} {value}'])
    return PlainTextResponse('\n'.join(lines) + '\n', media_type='text/plain; version=0.0.4')
