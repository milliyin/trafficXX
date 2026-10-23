from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from trafficxx.db import connect


def fail(code, status=409):
    raise HTTPException(status_code=status, detail={'code': code})


def lock_event(conn, event_id):
    event = conn.execute('SELECT * FROM events WHERE id = %s FOR UPDATE', (event_id,)).fetchone()
    if not event:
        fail('EVENT_NOT_FOUND', 404)
    return event


def emit(conn, reservation):
    conn.execute(
        'INSERT INTO outbox(reservation_id, event_type, payload) VALUES (%s, %s, %s)',
        (reservation['id'], reservation['status'], Jsonb({
            'reservation_id': str(reservation['id']), 'status': reservation['status'],
            'event_id': reservation['event_id'],
        })),
    )


def expire_event(conn, event_id):
    # Caller holds the event lock. All inventory writers use this same lock order.
    expired = conn.execute(
        "UPDATE reservations SET status = 'EXPIRED' "
        "WHERE event_id = %s AND status = 'HELD' AND expires_at <= clock_timestamp() RETURNING *",
        (event_id,),
    ).fetchall()
    for row in expired:
        conn.execute('UPDATE ticket_units SET reservation_id = NULL WHERE reservation_id = %s', (row['id'],))
        emit(conn, row)
    return len(expired)


def view(conn, row):
    # Preserve requested seats even after cancellation releases the units.
    return {
        'id': row['id'], 'event_id': row['event_id'], 'status': row['status'],
        'quantity': row['quantity'], 'seats': row['request'].get('seats'),
        'created_at': row['created_at'], 'expires_at': row['expires_at'],
    }


def reserve(customer, key, request, *, connection_factory=None):
    canonical = request.model_dump(exclude_none=True)
    # API supplies one pool lease; workers/direct callers retain connect().
    # Keep the entire operation in this one transaction, without write replay.
    with (connection_factory or connect)() as conn:
        # Serialize matching keys across processes before touching inventory.
        # Hash collisions only add contention; uniqueness is enforced separately.
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                     (f'{len(customer)}:{customer}{key}',))
        previous = conn.execute(
            'SELECT * FROM reservations WHERE customer_id = %s AND idempotency_key = %s',
            (customer, key),
        ).fetchone()
        if previous and previous['request'] != canonical:
            fail('IDEMPOTENCY_KEY_REUSED')
        event = lock_event(conn, request.event_id)
        expire_event(conn, request.event_id)
        if previous:
            row = conn.execute('SELECT * FROM reservations WHERE id = %s', (previous['id'],)).fetchone()
            return view(conn, row), False
        if event['inventory_type'] == 'SEATED':
            if request.seats is None:
                fail('SEATS_REQUIRED', 422)
            units = conn.execute(
                'SELECT unit_number FROM ticket_units WHERE event_id = %s '
                'AND seat_label = ANY(%s) AND reservation_id IS NULL ORDER BY unit_number',
                (request.event_id, request.seats),
            ).fetchall()
            quantity = len(request.seats)
        else:
            if request.quantity is None:
                fail('QUANTITY_REQUIRED', 422)
            quantity = request.quantity
            units = conn.execute(
                'SELECT unit_number FROM ticket_units WHERE event_id = %s '
                'AND reservation_id IS NULL ORDER BY unit_number LIMIT %s',
                (request.event_id, quantity),
            ).fetchall()
        if len(units) != quantity:
            fail('SEAT_UNAVAILABLE' if request.seats else 'INSUFFICIENT_INVENTORY')
        row = conn.execute(
            'INSERT INTO reservations(id, event_id, customer_id, idempotency_key, request, '
            'quantity, status, expires_at) VALUES (%s, %s, %s, %s, %s, %s, '
            "'HELD', clock_timestamp() + interval '5 minutes') RETURNING *",
            (uuid4(), request.event_id, customer, key, Jsonb(canonical), quantity),
        ).fetchone()
        conn.execute(
            'UPDATE ticket_units SET reservation_id = %s WHERE event_id = %s AND unit_number = ANY(%s)',
            (row['id'], request.event_id, [unit['unit_number'] for unit in units]),
        )
        emit(conn, row)
        return view(conn, row), True


def reservation(customer, reservation_id, action=None, *, connection_factory=None):
    error = None
    with (connection_factory or connect)() as conn:
        row = conn.execute('SELECT * FROM reservations WHERE id = %s AND customer_id = %s',
                           (reservation_id, customer)).fetchone()
        if not row:
            fail('RESERVATION_NOT_FOUND', 404)
        lock_event(conn, row['event_id'])
        expire_event(conn, row['event_id'])
        row = conn.execute('SELECT * FROM reservations WHERE id = %s', (reservation_id,)).fetchone()
        if action and row['status'] != action:
            if row['status'] != 'HELD':
                error = 'RESERVATION_' + row['status']
            else:
                # Check database wall time again at the actual state transition.
                updated = conn.execute(
                    'UPDATE reservations SET status = %s WHERE id = %s '
                    "AND status = 'HELD' AND expires_at > clock_timestamp() RETURNING *",
                    (action, reservation_id),
                ).fetchone()
                if updated is None:
                    expire_event(conn, row['event_id'])
                    error = 'RESERVATION_EXPIRED'
                else:
                    row = updated
                    if action == 'CANCELLED':
                        conn.execute('UPDATE ticket_units SET reservation_id = NULL WHERE reservation_id = %s',
                                     (reservation_id,))
                    emit(conn, row)
        result = view(conn, row)
    # Commit expiry and inventory release before returning a conflict.
    if error:
        fail(error)
    return result


def sweep():
    with connect() as conn:
        events = conn.execute("SELECT DISTINCT event_id FROM reservations WHERE status = 'HELD' "
                              'AND expires_at <= clock_timestamp()').fetchall()
    count = 0
    for event in events:
        with connect() as conn:
            lock_event(conn, event['event_id'])
            count += expire_event(conn, event['event_id'])
    return count
