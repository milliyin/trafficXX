"""AKS consumer private connectivity/link authorization; peek only, with send authorization denied."""
import base64
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import ssl

CLIENT = '35057245-c3ec-4eba-b182-60549501df42'
TENANT = 'd2828580-3217-43ad-9f3f-8d430998b86e'
NAMESPACE = 'trafficxx-sb-e8708a85.servicebus.windows.net'
QUEUE = 'reservation-events'
PRIVATE_NETWORK = ipaddress.ip_network('10.40.17.0/24')
CURRENT_CHECK = 'identity_preflight'


def check():
    global CURRENT_CHECK
    from azure.identity import WorkloadIdentityCredential
    from azure.servicebus import ServiceBusClient, ServiceBusSubQueue
    from azure.servicebus.exceptions import ServiceBusAuthorizationError

    if (os.environ.get('AZURE_CLIENT_ID') != CLIENT or os.environ.get('AZURE_TENANT_ID') != TENANT
            or os.environ.get('SERVICEBUS_FULLY_QUALIFIED_NAMESPACE') != NAMESPACE
            or os.environ.get('SERVICEBUS_QUEUE') != QUEUE
            or os.environ.get('SERVICEBUS_CONNECTION_STRING')):
        raise ValueError('Unexpected identity or broker configuration')
    token_file = os.environ['AZURE_FEDERATED_TOKEN_FILE']
    assertion = Path(token_file).read_text().strip()
    payload = assertion.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    if claims.get('sub') != 'system:serviceaccount:trafficxx:consumer':
        raise ValueError('Unexpected service account')
    ca = '/etc/ssl/certs/ca-certificates.crt'
    if not Path(ca).is_file():
        raise ValueError('Missing public CA bundle')
    CURRENT_CHECK = 'private_dns'
    addresses = sorted({info[4][0] for info in socket.getaddrinfo(
        NAMESPACE, 5671, socket.AF_UNSPEC, socket.SOCK_STREAM)})
    if not addresses or any(ipaddress.ip_address(ip) not in PRIVATE_NETWORK for ip in addresses):
        raise ValueError('Namespace must resolve only to the expected private endpoint subnet')
    CURRENT_CHECK = 'amqp_tls'
    context = ssl.create_default_context(cafile=ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((NAMESPACE, 5671), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=NAMESPACE) as tls:
            peer = tls.getpeername()[0]
            if ipaddress.ip_address(peer) not in PRIVATE_NETWORK:
                raise ValueError('TLS peer is outside expected private subnet')
            tls_version = tls.version()
            if tls_version not in ('TLSv1.2', 'TLSv1.3'):
                raise ValueError('Unexpected negotiated TLS version')
    CURRENT_CHECK = 'receiver_link'
    with WorkloadIdentityCredential(tenant_id=TENANT, client_id=CLIENT,
                                    token_file_path=token_file) as credential:
        with ServiceBusClient(NAMESPACE, credential, retry_total=0,
                              logging_enable=False, auth_timeout=10) as bus:
            # Prefetch remains disabled. Peek browses without locking/settling.
            with bus.get_queue_receiver(QUEUE, prefetch_count=0, max_wait_time=1,
                                        socket_timeout=10) as receiver:
                receiver.peek_messages(max_message_count=1, timeout=10)
            CURRENT_CHECK = 'dead_letter_peek'
            with bus.get_queue_receiver(QUEUE, sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                                        prefetch_count=0, max_wait_time=1,
                                        socket_timeout=10) as receiver:
                receiver.peek_messages(max_message_count=1, timeout=10)
            CURRENT_CHECK = 'send_denial'
            try:
                with bus.get_queue_sender(QUEUE, socket_timeout=10) as sender:
                    sender.create_message_batch()
            except ServiceBusAuthorizationError:
                pass
            else:
                raise ValueError('Consumer unexpectedly has send-link access')
    return {'result': 'SERVICEBUS_CONSUMER_ACCESS_OK', 'namespace': NAMESPACE,
            'queue': QUEUE, 'private_ips': addresses, 'tls_peer': peer,
            'tls_version': tls_version, 'receiver_link_open': True,
            'queue_peek_allowed': True, 'dead_letter_peek_allowed': True,
            'send_denied': True, 'broker_messages_sent': 0,
            'broker_messages_received': 0,
            'client_id': CLIENT}


def main():
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(check()))
        return 0
    except Exception as error:
        # No raw broker/identity error bodies, projected assertions or tokens.
        print(json.dumps({'result': 'SERVICEBUS_CONSUMER_ACCESS_FAILED',
                          'check': CURRENT_CHECK, 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
