import logging
import time

from azure.core.exceptions import AzureError
from psycopg import Error

from trafficxx.broker import client, queue_name, send_event
from trafficxx.messaging import heartbeat, publish_one


def serve_connection(send, publish=publish_one, record=heartbeat,
                     clock=time.monotonic, sleep=time.sleep):
    """Retire an unused sender before Service Bus's ten-minute idle window.

    Return between publish_one transactions, before selecting another row;
    the caller closes both sender and client and opens a fresh connection.
    """
    last_success = clock()
    while True:
        idle_seconds = clock() - last_success
        if idle_seconds >= 480:
            logging.info('publisher_connection_refresh reason=idle idle_seconds=%s', int(idle_seconds))
            return
        outcome = publish(send)
        record('publisher', 'BROKER_SEND_FAILED' if outcome == 'retry' else None)
        if outcome != 'idle':
            logging.info('outbox_publish outcome=%s', outcome)
        if outcome == 'published':
            last_success = clock()
        else:
            sleep(1)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('azure').setLevel(logging.WARNING)
    while True:
        try:
            with client() as bus, bus.get_queue_sender(queue_name()) as sender:
                serve_connection(lambda event: send_event(sender, event))
        except (AzureError, Error) as error:
            logging.warning('publisher_reconnect error_class=%s', type(error).__name__)
            try:
                heartbeat('publisher', type(error).__name__)
            except Error:
                pass
            time.sleep(5)


if __name__ == '__main__':
    main()
