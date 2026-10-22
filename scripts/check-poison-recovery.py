"""Read-only valid-work recovery and retained diagnostic DLQ audit evidence."""
import json
import logging
from pathlib import Path
import runpy

MESSAGE_ID = 'bd7fe2e5-7631-4366-9594-69b82ba8d07a'


def verify(connection, state):
    if (state.get('result') != 'CONSUMER_DRAIN_OK' or state.get('total_count') != 16
            or state.get('inbox_count') != 16 or state.get('expected_projection_count') != 8):
        raise ValueError('Expected one new hold/cancel flow after the 14-event checkpoint')
    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    rows = connection.execute(
        'SELECT queue_name, sequence_number, reason FROM public.observed_dead_letters '
        'WHERE message_id = %s', (MESSAGE_ID,)).fetchall()
    if (len(rows) != 1 or rows[0]['queue_name'] != 'reservation-events'
            or rows[0]['sequence_number'] != 16 or rows[0]['reason'] != 'INVALID_EVENT_SCHEMA'):
        raise ValueError('Expected retained diagnostic dead-letter audit record')
    inbox = connection.execute(
        'SELECT count(*) AS count FROM public.consumer_inbox WHERE message_id = %s',
        (MESSAGE_ID,)).fetchone()
    if not inbox or inbox['count'] != 0:
        raise ValueError('Invalid diagnostic must not enter the inbox')
    return {'result': 'POISON_RECOVERY_OK', 'diagnostic_message_id': MESSAGE_ID,
            'diagnostic_inbox_rows': 0, 'audit_record_retained': True,
            'audit_sequence_number': 16, 'audit_reason': rows[0]['reason'],
            'stored_state': state, 'read_only': True}


def main():
    logging.disable(logging.CRITICAL)
    try:
        observer = runpy.run_path(str(Path(__file__).with_name('check-consumer-drain.py')))
        state = observer['run']()
        from trafficxx.db import connect
        with connect() as connection:
            result = verify(connection, state)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'POISON_RECOVERY_FAILED', 'error_type': type(error).__name__,
                          'note': 'Inspect the normal flow and retained diagnostic audit; do not resend the poison message.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
