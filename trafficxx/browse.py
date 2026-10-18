from fastapi import FastAPI

from trafficxx.db import connect
from trafficxx.health import register
from trafficxx.service import fail

app = FastAPI(title='trafficXX Browse API', version='0.1.0')
register(app)


@app.get('/events')
def events():
    with connect() as conn:
        return conn.execute('SELECT * FROM events ORDER BY id').fetchall()


@app.get('/events/{event_id}/availability')
def availability(event_id: str):
    with connect() as conn:
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
