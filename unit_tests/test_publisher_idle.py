"""Idle sender retirement, successful activity and retry behavior with fake clocks."""
import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch


class FakeAzureError(Exception):
    pass


class FakeDatabaseError(Exception):
    pass


class IdlePublisher(unittest.TestCase):
    def setUp(self):
        exceptions = types.ModuleType('azure.core.exceptions')
        exceptions.AzureError = FakeAzureError
        psycopg = types.ModuleType('psycopg')
        psycopg.Error = FakeDatabaseError
        broker = types.ModuleType('trafficxx.broker')
        broker.client = MagicMock()
        broker.queue_name = MagicMock(return_value='reservation-events')
        broker.send_event = MagicMock()
        messaging = types.ModuleType('trafficxx.messaging')
        messaging.heartbeat = MagicMock()
        messaging.publish_one = MagicMock()
        modules = patch.dict('sys.modules', {'azure.core.exceptions': exceptions,
            'psycopg': psycopg, 'trafficxx.broker': broker, 'trafficxx.messaging': messaging})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location(
            'publisher_idle', Path(__file__).resolve().parents[1] / 'trafficxx/publisher.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.now = 0

    def test_expired_idle_sender_is_not_used_to_select_another_outbox_row(self):
        def publish(send):
            self.now = 480
            return 'idle'
        publish = MagicMock(side_effect=publish)
        record = MagicMock()
        with self.assertLogs(level='INFO') as captured:
            self.module.serve_connection(None, publish, record, lambda: self.now, lambda _: None)
        publish.assert_called_once()
        record.assert_called_once_with('publisher', None)
        self.assertIn('publisher_connection_refresh reason=idle', '\n'.join(captured.output))

    def test_successful_send_resets_idle_age_and_busy_sender_is_not_retired_early(self):
        steps = iter([('published', 400), ('idle', 100), ('published', 200), ('idle', 480)])

        def publish(send):
            outcome, elapsed = next(steps)
            self.now += elapsed
            return outcome
        publish = MagicMock(side_effect=publish)
        self.module.serve_connection(None, publish, MagicMock(), lambda: self.now, lambda _: None)
        self.assertEqual(publish.call_count, 4)
        self.assertEqual(self.now, 1180)

    def test_retry_keeps_existing_error_heartbeat_sleep_and_can_then_publish(self):
        outcomes = iter(['retry', 'published', 'idle'])
        sleeps = []

        def publish(send):
            value = next(outcomes)
            if value == 'idle': self.now += 480
            return value

        def sleep(seconds):
            sleeps.append(seconds)
            self.now += seconds
        record = MagicMock()
        self.module.serve_connection(None, publish, record, lambda: self.now, sleep)
        self.assertEqual([call.args for call in record.call_args_list], [
            ('publisher', 'BROKER_SEND_FAILED'), ('publisher', None), ('publisher', None)])
        self.assertEqual(sleeps, [1, 1])

    def test_database_failure_is_not_swallowed_by_idle_loop(self):
        with self.assertRaises(FakeDatabaseError):
            self.module.serve_connection(None, MagicMock(side_effect=FakeDatabaseError()),
                                         MagicMock(), lambda: self.now, lambda _: None)

    def test_session_return_closes_sender_and_client_before_reopening(self):
        events = []

        class Context:
            def __init__(self, name): self.name = name
            def __enter__(self): events.append('enter-' + self.name); return self
            def __exit__(self, *args): events.append('exit-' + self.name)
            def get_queue_sender(self, name): return Context('sender')

        calls = []

        def client():
            if calls: raise KeyboardInterrupt()
            calls.append(True)
            return Context('client')
        with patch.object(self.module, 'client', side_effect=client), patch.object(
                self.module, 'serve_connection', return_value=None):
            with self.assertRaises(KeyboardInterrupt): self.module.main()
        self.assertEqual(events, ['enter-client', 'enter-sender', 'exit-sender', 'exit-client'])
