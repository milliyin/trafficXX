"""Read-only Browse SQL; one statement returns event and available unit labels."""

AVAILABILITY_SQL = """
SELECT e.*, ARRAY(
    SELECT u.seat_label
    FROM ticket_units u
    LEFT JOIN reservations r ON r.id = u.reservation_id
    WHERE u.event_id = e.id AND (
        u.reservation_id IS NULL OR
        (r.status = 'HELD' AND r.expires_at <= clock_timestamp())
    )
    ORDER BY u.unit_number
) AS _available_seats
FROM events e
WHERE e.id = %s
"""


def fetch_availability(conn, event_id):
    row = conn.execute(AVAILABILITY_SQL, (event_id,)).fetchone()
    if row is None:
        return None
    event = dict(row)
    labels = event.pop('_available_seats')
    return {**event, 'available': len(labels),
            'seats': labels if event['inventory_type'] == 'SEATED' else None}
