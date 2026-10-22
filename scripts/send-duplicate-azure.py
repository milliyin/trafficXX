"""Resend one existing event with its original identity; no outbox edits or auto retry."""
import base64
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path

CLIENT = 'abd012d6-9e05-415d-b3f5-8c24bee54576'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
NAMESPACE = 'trafficxx-sb-e8708a85.servicebus.windows.net'
QUEUE = 'reservation-events'
CURRENT_CHECK = 'preflight'
STATE = {}


def preflight():
    expected = {'AZURE_CLIENT_ID': CLIENT, 'AZURE_TENANT_ID': TENANT,
                'AZURE_TOKEN_CREDENTIALS': 'WorkloadIdentityCredential',
                'DATABASE_AUTH_MODE': 'workload_identity', 'PGUSER': 'trafficxx-lab-publisher',
                'PGHOST': 'trafficxx-pg-e8708a85.postgres.database.azure.com', 'PGDATABASE': 'trafficxx',
                'SERVICEBUS_FULLY_QUALIFIED_NAMESPACE': NAMESPACE, 'SERVICEBUS_QUEUE': QUEUE}
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise ValueError('Unexpected identity/database/broker configuration')
    if os.environ.get('SERVICEBUS_CONNECTION_STRING') or os.environ.get('DATABASE_URL'):
        raise ValueError('Credential URLs/connection strings must not be configured')
    if version('azure-identity') != '1.26.0':
        raise ValueError('Review credential selection before using a different SDK version')
    assertion = Path(os.environ['AZURE_FEDERATED_TOKEN_FILE']).read_text().strip()
    payload = assertion.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('sub') != 'system:serviceaccount:trafficxx:publisher':
        raise ValueError('Unexpected projected service account')


def run():
    global CURRENT_CHECK
    from datetime import datetime, timezone
    STATE.clear()
    STATE.update(send_calls=0, send_acknowledged=False, outbox_edited=False)
    CURRENT_CHECK = 'preflight'
    preflight()
    from trafficxx.broker import client, queue_name, send_event
    from trafficxx.messaging import envelope
    from trafficxx.db import connect

    CURRENT_CHECK = 'read_original_event'
    with connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        row = connection.execute('SELECT * FROM public.outbox WHERE id = %s', (3,)).fetchone()
        if (not row or row['published_at'] is None
                or str(row['message_id']) != '151af373-f74d-4bc4-8c14-e3b6cf13aa51'):
            raise ValueError('Expected the original published event from the baseline')
        event = envelope(row)
    STATE.update(outbox_id=event.outbox_id, message_id=str(event.message_id))
    CURRENT_CHECK = 'send_same_event'
    with client() as bus, bus.get_queue_sender(queue_name()) as sender:
        STATE['send_started_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        STATE['send_calls'] = 1
        send_event(sender, event)
        STATE['send_acknowledged'] = True
    return {'result': 'DUPLICATE_SEND_OK', **STATE,
            'note': 'Broker accepted the resend; consumer duplicate handling and unchanged state need separate proof.'}


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'DUPLICATE_SEND_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, **STATE,
                          'note': 'Sending may have succeeded before a timeout/SQL failure. '
                                  'Inspect the printed outbox/message IDs before retrying; '
                                  'do not replace the durable message ID.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
