"""One REAL outbox publication through the application broker path; no auto retry."""
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
    STATE.clear()
    STATE.update(send_calls=0, send_acknowledged=False, publication_committed=False)
    CURRENT_CHECK = 'preflight'
    preflight()
    from trafficxx.broker import client, queue_name, send_event
    from trafficxx.messaging import publish_one, heartbeat
    from trafficxx.db import connect

    CURRENT_CHECK = 'broker_open'
    with client() as bus, bus.get_queue_sender(queue_name()) as sender:
        def send(event):
            if STATE['send_calls']:
                raise ValueError('Unexpected second send callback')
            STATE.update(send_calls=1, outbox_id=event.outbox_id,
                         message_id=str(event.message_id), event_status=event.status)
            send_event(sender, event)
            STATE['send_acknowledged'] = True

        CURRENT_CHECK = 'publish_one'
        STATE['publication_committed'] = None
        outcome = publish_one(send)
        STATE['publication_committed'] = outcome == 'published'
    if outcome != 'published' or not STATE['send_acknowledged']:
        raise ValueError('No acknowledged committed publication; inspect outcome before retry')
    CURRENT_CHECK = 'publication_readback'
    with connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        row = connection.execute(
            'SELECT id, message_id, published_at IS NOT NULL AS published, '
            'publish_attempts, last_error FROM public.outbox WHERE id = %s',
            (STATE['outbox_id'],)).fetchone()
        if (not row or row['id'] != STATE['outbox_id']
                or str(row['message_id']) != STATE['message_id'] or not row['published']
                or row['publish_attempts'] < 1 or row['last_error'] is not None):
            raise ValueError('Unexpected persisted publication state')
    CURRENT_CHECK = 'heartbeat'
    heartbeat('publisher')
    with connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        beat = connection.execute('SELECT name, last_error FROM public.worker_heartbeats '
                                  'WHERE name = %s', ('publisher',)).fetchone()
        if not beat or beat['name'] != 'publisher' or beat['last_error'] is not None:
            raise ValueError('Publisher heartbeat not verified')
    return {'result': 'PUBLISHER_SEND_OK', 'namespace': NAMESPACE, 'queue': QUEUE,
            **STATE, 'heartbeat_recorded': True,
            'broker_credential': 'WorkloadIdentityCredential'}


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'PUBLISHER_SEND_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, **STATE,
                          'note': 'Sending may have succeeded before a timeout/SQL failure. '
                                  'Inspect the printed outbox/message IDs before retrying; '
                                  'do not replace the durable message ID.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
