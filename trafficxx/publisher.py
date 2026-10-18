import logging
import time

from azure.core.exceptions import AzureError
from psycopg import Error

from trafficxx.broker import client, queue_name, send_event
from trafficxx.messaging import heartbeat, publish_one


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('azure').setLevel(logging.WARNING)
    while True:
        try:
            with client() as bus, bus.get_queue_sender(queue_name()) as sender:
                while True:
                    outcome = publish_one(lambda event: send_event(sender, event))
                    heartbeat('publisher', 'BROKER_SEND_FAILED' if outcome == 'retry' else None)
                    if outcome != 'idle':
                        logging.info('outbox_publish outcome=%s', outcome)
                    if outcome != 'published':
                        time.sleep(1)
        except (AzureError, Error) as error:
            logging.warning('publisher_reconnect error_class=%s', type(error).__name__)
            try:
                heartbeat('publisher', type(error).__name__)
            except Error:
                pass
            time.sleep(5)


if __name__ == '__main__':
    main()
