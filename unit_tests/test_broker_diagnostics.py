"""Transport-class logging without SDK dependencies or sensitive error details."""
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch


class FakeAzureError(Exception):
    pass


class FakePublishError(Exception):
    pass


class BrokerDiagnostics(unittest.TestCase):
    def setUp(self):
        identity = types.ModuleType('azure.identity')
        identity.DefaultAzureCredential = MagicMock()
        servicebus = types.ModuleType('azure.servicebus')
        servicebus.ServiceBusClient = MagicMock()
        servicebus.ServiceBusMessage = MagicMock()
        exceptions = types.ModuleType('azure.core.exceptions')
        exceptions.AzureError = FakeAzureError
        messaging = types.ModuleType('trafficxx.messaging')
        messaging.PublishError = FakePublishError
        modules = patch.dict('sys.modules', {'azure.identity': identity,
            'azure.servicebus': servicebus, 'azure.core.exceptions': exceptions,
            'trafficxx.messaging': messaging})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location(
            'broker_diagnostics', Path(__file__).resolve().parents[1] / 'trafficxx/broker.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.sender = MagicMock()
        self.event = MagicMock(status='HELD', message_id='fake-message-id')
        self.event.model_dump_json.return_value = 'PRIVATE_EVENT_BODY'

    def test_failed_send_logs_class_not_error_text_payload_or_traceback(self):
        self.sender.send_messages.side_effect = FakeAzureError('PRIVATE_TOKEN PRIVATE_ENDPOINT')
        with self.assertLogs('broker_diagnostics', level='WARNING') as captured:
            with self.assertRaises(FakePublishError) as error:
                self.module.send_event(self.sender, self.event)
        output = '\n'.join(captured.output)
        self.assertIn('broker_send_failed error_class=FakeAzureError', output)
        for private in ['PRIVATE_TOKEN', 'PRIVATE_ENDPOINT', 'PRIVATE_EVENT_BODY', 'fake-message-id', 'Traceback']:
            self.assertNotIn(private, output)
        self.assertEqual(str(error.exception), 'BROKER_SEND_FAILED')
        self.assertTrue(error.exception.__suppress_context__)
        self.sender.send_messages.assert_called_once()

    def test_success_does_not_log_failure_or_add_an_extra_send(self):
        with self.assertNoLogs('broker_diagnostics', level='WARNING'):
            self.module.send_event(self.sender, self.event)
        self.sender.send_messages.assert_called_once()

    def test_programming_error_is_not_reclassified_as_retryable_broker_failure(self):
        self.sender.send_messages.side_effect = TypeError('Programming error')
        with self.assertNoLogs('broker_diagnostics', level='WARNING'):
            with self.assertRaises(TypeError): self.module.send_event(self.sender, self.event)
