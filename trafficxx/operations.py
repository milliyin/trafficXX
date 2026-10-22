import os

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from trafficxx.db import connect
from trafficxx.health import register
from trafficxx.monitoring import evaluate, expected_workers, render_metrics

app = FastAPI(title='trafficXX Operations', version='0.3.0')
register(app, readiness_query='SELECT 1')


def snapshot():
    with connect() as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
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
    return evaluate(values, workers, os.environ.get('BACKLOG_ALERT_SECONDS', '30'),
                    expected_workers(os.environ.get('EXPECTED_HEARTBEAT_WORKERS', '')))


@app.get('/backlog')
def backlog():
    return snapshot()


@app.get('/metrics', response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(render_metrics(snapshot()), media_type='text/plain; version=0.0.4')
