"""Read-only stored-state evidence; never call reservation/expiry service code."""
import json
import os
from uuid import UUID


def observe(connection, reservation_id, phase):
    if phase not in ('held', 'expired'):
        raise ValueError('Unknown observation phase')
    connection.execute('SET TRANSACTION READ ONLY')
    session = connection.execute("""
        SELECT current_user AS identity, current_database() AS database, s.ssl,
               pg_has_role(current_user, 'azure_pg_admin', 'MEMBER') AS admin
        FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()
    """).fetchone()
    if (not session or session['identity'] != 'trafficxx-lab-migrator'
            or session['database'] != 'trafficxx' or not session['ssl'] or session['admin']):
        raise ValueError('Unexpected observer identity or TLS')
    row = connection.execute("""
        SELECT r.id, r.event_id, r.status, r.quantity, r.request,
               r.expires_at <= clock_timestamp() AS due,
               (SELECT count(*) FROM public.ticket_units u
                WHERE u.reservation_id = r.id) AS assignments,
               (SELECT count(*) FROM public.ticket_units u
                WHERE u.event_id = 'demo-seated' AND u.seat_label = 'A2'
                  AND u.reservation_id = r.id) AS matching_seat,
               (SELECT count(*) FROM public.outbox o
                WHERE o.reservation_id = r.id AND o.event_type = 'HELD') AS held_messages,
               (SELECT count(*) FROM public.outbox o
                WHERE o.reservation_id = r.id AND o.event_type = 'EXPIRED') AS expired_messages
        FROM public.reservations r WHERE r.id = %s
    """, (UUID(str(reservation_id)),)).fetchone()
    expected = ('HELD', False, 1, 1, 0) if phase == 'held' else ('EXPIRED', True, 0, 0, 1)
    if (not row or row['event_id'] != 'demo-seated' or row['quantity'] != 1
            or row['request'] != {'event_id': 'demo-seated', 'seats': ['A2']}
            or (row['status'], row['due'], row['assignments'], row['matching_seat'],
                row['expired_messages']) != expected or row['held_messages'] != 1):
        raise ValueError('Stored state does not match expected expiry phase')
    return {'result': 'EXPIRY_STATE_OK', 'phase': phase, 'reservation_id': str(row['id']),
            'status': row['status'], 'assignments': row['assignments'],
            'held_messages': row['held_messages'], 'expired_messages': row['expired_messages'],
            'observer_identity': session['identity'], 'read_only': True}


def main():
    from trafficxx.db import connect
    try:
        with connect() as connection:
            result = observe(connection, os.environ['CHECK_RESERVATION_ID'], os.environ['CHECK_PHASE'])
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'EXPIRY_STATE_FAILED', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
