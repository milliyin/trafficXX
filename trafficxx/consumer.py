import logging
import time

from azure.core.exceptions import AzureError
from azure.servicebus import ServiceBusSubQueue
from psycopg import Error

from trafficxx.broker import client, queue_name
from trafficxx.messaging import (
    InvalidEvent, heartbeat, parse_event, process_event, record_dead_letter, record_failure,
)


def dead_letter(receiver, message, queue, reason):
    receiver.dead_letter_message(message, reason=reason, error_description='Reservation event processing failed')
    record_dead_letter(queue, message, reason)
    return 'dead_letter'


def handle_message(receiver, message, queue, max_attempts=5):
    try:
        try:
            body = b''.join(message.body)
        except (TypeError, ValueError):
            raise InvalidEvent('INVALID_EVENT_BODY') from None
        if len(body) > 65536:
            raise InvalidEvent('EVENT_TOO_LARGE')
        event = parse_event(body, message.message_id)
        created = process_event(event)
    except InvalidEvent as error:
        return dead_letter(receiver, message, queue, str(error))
    except Error as error:
        try:
            record_failure(message.message_id, type(error).__name__)
        except Error:
            pass  # A database outage can prevent recording its own failure.
        attempt = int(message.delivery_count or 0) + 1
        if attempt >= max_attempts:
            return dead_letter(receiver, message, queue, 'PROCESSING_RETRIES_EXHAUSTED')
        time.sleep(min(2, 0.25 * 2 ** min(attempt, 3)))
        receiver.abandon_message(message)
        return 'retry'
    # Inbox and downstream state are committed before we acknowledge the broker.
    receiver.complete_message(message)
    return 'processed' if created else 'duplicate'


def observe_dead_letters(bus, queue):
    # Non-destructive, bounded sample. This is not an exact native DLQ count.
    with bus.get_queue_receiver(queue_name=queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as receiver:
        messages = receiver.peek_messages(max_message_count=100, timeout=5)
        for message in messages:
            record_dead_letter(queue, message)
        return len(messages)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('azure').setLevel(logging.WARNING)
    queue = queue_name()
    while True:
        try:
            with client() as bus, bus.get_queue_receiver(queue_name=queue, prefetch_count=0) as receiver:
                last_scan = 0
                while True:
                    heartbeat('consumer')
                    if time.monotonic() - last_scan >= 15:
                        observe_dead_letters(bus, queue)
                        last_scan = time.monotonic()
                    for message in receiver.receive_messages(max_message_count=1, max_wait_time=5):
                        outcome = handle_message(receiver, message, queue)
                        logging.info('event_delivery outcome=%s message_id=%s', outcome, message.message_id)
        except (AzureError, Error) as error:
            logging.warning('consumer_reconnect error_class=%s', type(error).__name__)
            try:
                heartbeat('consumer', type(error).__name__)
            except Error:
                pass
            time.sleep(5)


if __name__ == '__main__':
    main()
