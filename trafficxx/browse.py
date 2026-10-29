from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from trafficxx.browse_db import BrowseDatabase
from trafficxx.browse_metrics import render
from trafficxx.browse_queries import fetch_availability
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
register(app, connection_factory=database.read_connection)


@app.get('/internal/database-pool', include_in_schema=False)
def database_pool():
    return database.statistics()


@app.get('/metrics', include_in_schema=False, response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(render(database.statistics()['statistics']),
                             media_type='text/plain; version=0.0.4')


@app.get('/events')
def events():
    with database.read_connection() as conn:
        return conn.execute('SELECT * FROM events ORDER BY id').fetchall()


@app.get('/events/{event_id}/availability')
def availability(event_id: str):
    with database.read_connection() as conn:
        with database.timings.measure('availability_query'):
            event = fetch_availability(conn, event_id)
        if not event:
            fail('EVENT_NOT_FOUND', 404)
        return event
