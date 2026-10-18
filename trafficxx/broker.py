import os
from contextlib import ExitStack, contextmanager
from urllib.request import urlopen
import time

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.core.exceptions import AzureError

from trafficxx.messaging import PublishError


def queue_name():
    return os.environ.get('SERVICEBUS_QUEUE', 'reservation-events')


@contextmanager
def client():
    with ExitStack() as stack:
        connection = os.environ.get('SERVICEBUS_CONNECTION_STRING')
        if connection:
            bus = ServiceBusClient.from_connection_string(connection, retry_total=0)
        else:
            namespace = os.environ.get('SERVICEBUS_FULLY_QUALIFIED_NAMESPACE')
            if not namespace:
                raise RuntimeError('Configure Service Bus connection string or namespace')
            credential = stack.enter_context(DefaultAzureCredential())
            bus = ServiceBusClient(namespace, credential, retry_total=0)
        yield stack.enter_context(bus)


def send_event(sender, event):
    message = ServiceBusMessage(event.model_dump_json(), message_id=str(event.message_id),
                                content_type='application/json', subject='reservation.' + event.status.lower())
    try:
        sender.send_messages(message, timeout=10)
    except AzureError:
        raise PublishError('BROKER_SEND_FAILED') from None


def wait_for_emulator(timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen('http://servicebus:5300/health', timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeError('Service Bus emulator did not become healthy before timeout')
