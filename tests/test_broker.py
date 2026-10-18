"""Optional real-AMQP tests against the dedicated emulator test queue."""
import os
import time
from uuid import uuid4

import pytest
from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue
from psycopg import OperationalError

from trafficxx.broker import client, queue_name, send_event, wait_for_emulator
from trafficxx.consumer import handle_message, observe_dead_letters
from trafficxx.db import connect
from trafficxx.messaging import envelope, process_event, publish_one
from trafficxx.models import ReservationRequest
from trafficxx.service import reserve

pytestmark = pytest.mark.skipif(os.environ.get('RUN_SERVICEBUS_TESTS') != '1',
                                reason='Run the broker-tests Compose service for real AMQP tests')


def drain(bus, queue, sub_queue=None):
    with bus.get_queue_receiver(queue_name=queue, sub_queue=sub_queue) as receiver:
        for _ in range(20):
            messages = receiver.receive_messages(max_message_count=50, max_wait_time=1)
            if not messages:
                return
            for message in messages:
                receiver.complete_message(message)
    pytest.fail('Dedicated test queue did not drain within 1,000 messages')


@pytest.fixture(autouse=True)
def emulator_bus(fresh_database):
    queue = queue_name()
    assert queue == 'reservation-test-events', 'Refusing to drain a non-test queue'
    wait_for_emulator()
    with client() as bus:
        drain(bus, queue)
        drain(bus, queue, ServiceBusSubQueue.DEAD_LETTER)
        yield bus
        drain(bus, queue)
        drain(bus, queue, ServiceBusSubQueue.DEAD_LETTER)


def event():
    reserve('broker-test', str(uuid4()), ReservationRequest(event_id='general', quantity=1))
    with connect() as conn:
        return envelope(conn.execute('SELECT * FROM outbox ORDER BY id DESC LIMIT 1').fetchone())


def receive_one(receiver):
    messages = receiver.receive_messages(max_message_count=1, max_wait_time=5)
    assert messages, 'Expected a message from the emulator'
    return messages[0]


def test_real_publish_and_duplicate_delivery_have_one_effect(emulator_bus):
    event()
    captured = []
    with emulator_bus.get_queue_sender(queue_name()) as sender:
        def send(outgoing):
            captured.append(outgoing)
            send_event(sender, outgoing)
        assert publish_one(send) == 'published'
        send_event(sender, captured[0])
    with emulator_bus.get_queue_receiver(queue_name=queue_name()) as receiver:
        outcomes = [handle_message(receiver, receive_one(receiver), queue_name()) for _ in range(2)]
    assert outcomes == ['processed', 'duplicate']
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM consumer_inbox').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM reservation_updates').fetchone()['n'] == 1


def test_real_poison_message_reaches_dead_letter_queue(emulator_bus):
    with emulator_bus.get_queue_sender(queue_name()) as sender:
        sender.send_messages(ServiceBusMessage('not JSON', message_id=str(uuid4())), timeout=10)
    with emulator_bus.get_queue_receiver(queue_name=queue_name()) as receiver:
        assert handle_message(receiver, receive_one(receiver), queue_name()) == 'dead_letter'
    with emulator_bus.get_queue_receiver(queue_name=queue_name(),
                                         sub_queue=ServiceBusSubQueue.DEAD_LETTER) as receiver:
        incoming = receive_one(receiver)
        assert incoming.dead_letter_reason == 'INVALID_EVENT_SCHEMA'
        receiver.complete_message(incoming)


def test_real_redelivery_after_transient_failure_succeeds(emulator_bus, monkeypatch):
    outgoing = event()
    with emulator_bus.get_queue_sender(queue_name()) as sender:
        send_event(sender, outgoing)
    with emulator_bus.get_queue_receiver(queue_name=queue_name()) as receiver:
        incoming = receive_one(receiver)
        first_delivery = incoming.delivery_count
        def unavailable(event):
            raise OperationalError('Simulated transient database failure')
        with monkeypatch.context() as patch:
            patch.setattr('trafficxx.consumer.process_event', unavailable)
            patch.setattr('trafficxx.consumer.time.sleep', lambda _: None)
            assert handle_message(receiver, incoming, queue_name()) == 'retry'
        repeated = receive_one(receiver)
        assert repeated.delivery_count > first_delivery
        assert handle_message(receiver, repeated, queue_name()) == 'processed'


def test_real_redelivery_after_committed_effect_is_safe(emulator_bus):
    outgoing = event()
    with emulator_bus.get_queue_sender(queue_name()) as sender:
        send_event(sender, outgoing)
    with emulator_bus.get_queue_receiver(queue_name=queue_name()) as receiver:
        incoming = receive_one(receiver)
        assert process_event(outgoing)
        # Simulate loss of the consumer after DB commit: force redelivery without completion.
        receiver.abandon_message(incoming)
        assert handle_message(receiver, receive_one(receiver), queue_name()) == 'duplicate'


def test_native_delivery_limit_is_observed_without_consuming_dead_letters(emulator_bus):
    outgoing = event()
    with emulator_bus.get_queue_sender(queue_name()) as sender:
        send_event(sender, outgoing)
    with emulator_bus.get_queue_receiver(queue_name=queue_name()) as receiver:
        for _ in range(5):
            receiver.abandon_message(receive_one(receiver))
    deadline = time.monotonic() + 15
    observed = 0
    while time.monotonic() < deadline and not observed:
        observed = observe_dead_letters(emulator_bus, queue_name())
    assert observed == 1
    with connect() as conn:
        assert conn.execute('SELECT reason FROM observed_dead_letters').fetchone()['reason'] == 'MaxDeliveryCountExceeded'
