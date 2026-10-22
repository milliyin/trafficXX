"""One identifiable invalid lab message; bounded peek and targeted DLQ cleanup."""
import base64
import json
import logging
import os
from pathlib import Path
import time

TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
NAMESPACE = 'trafficxx-sb-e8708a85.servicebus.windows.net'
QUEUE = 'reservation-events'
MESSAGE_ID = 'bd7fe2e5-7631-4366-9594-69b82ba8d07a'
BODY = json.dumps({'schema_version': 999, 'diagnostic': 'trafficxx-poison-probe', 'message_id': MESSAGE_ID})
REASON = 'INVALID_EVENT_SCHEMA'
CURRENT_CHECK = 'preflight'
STATE = {}


def verify_message(message):
    if (str(message.message_id) != MESSAGE_ID or message.dead_letter_reason != REASON
            or b''.join(message.body).decode('utf-8') != BODY):
        raise ValueError('Refusing to handle a different dead-letter message')


def operate(bus, action, message_type, dead_letter_subqueue, sleep=time.sleep):
    global CURRENT_CHECK
    if action == 'send':
        CURRENT_CHECK = 'send_invalid_diagnostic'
        with bus.get_queue_sender(QUEUE, socket_timeout=10) as sender:
            STATE['send_calls'] = 1
            sender.send_messages(message_type(BODY, message_id=MESSAGE_ID,
                                  content_type='application/json', subject='diagnostic.invalid'), timeout=10)
            STATE['send_acknowledged'] = True
        return {'result': 'POISON_SEND_OK', **STATE}
    if action not in ('peek', 'cleanup'):
        raise ValueError('Unexpected diagnostic action')
    CURRENT_CHECK = 'peek_diagnostic_dead_letter'
    with bus.get_queue_receiver(QUEUE, sub_queue=dead_letter_subqueue, prefetch_count=0,
                                max_wait_time=2, socket_timeout=10) as receiver:
        for attempt in range(12):
            messages = receiver.peek_messages(max_message_count=2, timeout=10)
            if messages:
                break
            if attempt < 11:
                sleep(5)
        else:
            raise TimeoutError('Diagnostic dead letter was not observed')
        if len(messages) != 1:
            raise ValueError('Expected only the diagnostic dead letter; inspect before cleanup')
        verify_message(messages[0])
        sequence = messages[0].sequence_number
        if action == 'peek':
            return {'result': 'POISON_DLQ_PEEK_OK', 'message_id': MESSAGE_ID,
                    'reason': REASON, 'sequence_number': sequence, 'settled': False}
        CURRENT_CHECK = 'cleanup_only_diagnostic_dead_letter'
        received = receiver.receive_messages(max_message_count=1, max_wait_time=5)
        if len(received) != 1:
            raise ValueError('Expected one diagnostic message for cleanup')
        message = received[0]
        try:
            verify_message(message)
            if message.sequence_number != sequence:
                raise ValueError('Dead-letter sequence changed')
        except Exception:
            receiver.abandon_message(message)
            raise
        STATE['completion_attempted'] = True
        receiver.complete_message(message)
        STATE['completion_acknowledged'] = True
        return {'result': 'POISON_CLEANUP_OK', 'message_id': MESSAGE_ID,
                'reason': REASON, 'sequence_number': sequence, **STATE}


def run():
    global CURRENT_CHECK
    CURRENT_CHECK = 'preflight'
    STATE.clear()
    STATE.update(message_id=MESSAGE_ID, send_calls=0, send_acknowledged=False,
                 completion_attempted=False, completion_acknowledged=False)
    action = os.environ.get('POISON_ACTION')
    if action not in ('send', 'peek', 'cleanup'):
        raise ValueError('Configure send, peek or cleanup')
    role = 'publisher' if action == 'send' else 'consumer'
    client = ('abd012d6-9e05-415d-b3f5-8c24bee54576' if action == 'send'
              else '35057245-c3ec-4eba-b182-60549501df42')
    expected = {'AZURE_CLIENT_ID': client, 'AZURE_TENANT_ID': TENANT,
                'SERVICEBUS_FULLY_QUALIFIED_NAMESPACE': NAMESPACE, 'SERVICEBUS_QUEUE': QUEUE}
    if (any(os.environ.get(k) != v for k, v in expected.items())
            or os.environ.get('SERVICEBUS_CONNECTION_STRING')):
        raise ValueError('Unexpected broker identity or configuration')
    token_file = os.environ['AZURE_FEDERATED_TOKEN_FILE']
    payload = Path(token_file).read_text().strip().split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('sub') != 'system:serviceaccount:trafficxx:' + role:
        raise ValueError('Unexpected projected service account')
    ca = '/etc/ssl/certs/ca-certificates.crt'
    if not Path(ca).is_file():
        raise ValueError('Missing public CA bundle')
    from azure.identity import WorkloadIdentityCredential
    from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusSubQueue
    with WorkloadIdentityCredential(tenant_id=TENANT, client_id=client,
                                    token_file_path=token_file) as credential:
        with ServiceBusClient(NAMESPACE, credential, retry_total=0, logging_enable=False,
                              auth_timeout=10, connection_verify=ca) as bus:
            return operate(bus, action, ServiceBusMessage, ServiceBusSubQueue.DEAD_LETTER)


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run()))
        return 0
    except Exception as error:
        print(json.dumps({'result': 'POISON_DIAGNOSTIC_FAILED', 'check': CURRENT_CHECK,
                          'error_type': type(error).__name__, **STATE,
                          'note': 'Inspect this diagnostic ID before retrying an ambiguous send or completion.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
