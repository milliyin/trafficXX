from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from trafficxx.browse_db import BrowseDatabase
from trafficxx.browse_metrics import render
from trafficxx.health import register
from trafficxx.service import fail

database = BrowseDatabase()


@asynccontextmanager
async def lifespan(app):
    database.open()
    try:
        yield
    finally:
        database.close()


app = FastAPI(title='trafficXX Browse API', version='0.1.0', lifespan=lifespan)
register(app, connection_factory=database.connection)


@app.get('/internal/database-pool', include_in_schema=False)
def database_pool():
    return database.statistics()


@app.get('/metrics', include_in_schema=False, response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(render(database.statistics()['statistics']),
                             media_type='text/plain; version=0.0.4')


@app.get('/events')
def events():
    with database.connection() as conn:
        return conn.execute('SELECT * FROM events ORDER BY id').fetchall()


@app.get('/events/{event_id}/availability')
def availability(event_id: str):
    with database.connection() as conn:
        event = conn.execute('SELECT * FROM events WHERE id = %s', (event_id,)).fetchone()
        if not event:
            fail('EVENT_NOT_FOUND', 404)
        # Expired holds are logically free even before the cleanup worker runs.
        units = conn.execute(
            'SELECT u.seat_label FROM ticket_units u LEFT JOIN reservations r ON r.id = u.reservation_id '
            'WHERE u.event_id = %s AND (u.reservation_id IS NULL OR '
            "(r.status = 'HELD' AND r.expires_at <= clock_timestamp())) ORDER BY u.unit_number",
            (event_id,),
        ).fetchall()
        return {**event, 'available': len(units),
                'seats': [u['seat_label'] for u in units] if event['inventory_type'] == 'SEATED' else None}
