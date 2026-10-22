"""Offline one-shot orchestration and ambiguous-send failure reporting."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    'publish_one_azure', Path(__file__).resolve().parents[1] / 'scripts/publish-one-azure.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PublishOneAzure(unittest.TestCase):
    def scenario(self, mode='success'):
        broker = types.ModuleType('trafficxx.broker')
        messaging = types.ModuleType('trafficxx.messaging')
        database = types.ModuleType('trafficxx.db')
        bus = MagicMock()
        bus.__enter__.return_value = bus
        broker.client = MagicMock(return_value=bus)
        broker.queue_name = lambda: 'reservation-events'
        broker.send_event = MagicMock()
        if mode == 'close-error':
            bus.__exit__.side_effect = RuntimeError('DO_NOT_LOG_RAW')
        event = types.SimpleNamespace(outbox_id=123, message_id='stable-message-id', status='HELD')

        def publish(send):
            if mode == 'idle':
                return 'idle'
            send(event)
            if mode == 'commit-error':
                raise RuntimeError('DO_NOT_LOG_RAW')
            if mode == 'double-send':
                send(event)
            return 'published'

        messaging.publish_one = MagicMock(side_effect=publish)
        messaging.heartbeat = MagicMock()
        if mode == 'heartbeat-error':
            messaging.heartbeat.side_effect = RuntimeError('DO_NOT_LOG_RAW')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        database.connect = MagicMock(return_value=connection)

        def execute(sql, params=None):
            cursor = MagicMock()
            if 'FROM public.outbox' in sql:
                cursor.fetchone.return_value = dict(id=123, message_id='stable-message-id',
                                                   published=mode != 'wrong-readback',
                                                   publish_attempts=1, last_error=None)
            else:
                cursor.fetchone.return_value = dict(name='publisher', last_error=None)
            return cursor

        connection.execute.side_effect = execute
        claims = {'sub': 'system:serviceaccount:trafficxx:publisher'}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        path = MagicMock()
        path.read_text.return_value = 'header.' + payload + '.signature'
        env = dict(AZURE_CLIENT_ID=module.CLIENT, AZURE_TENANT_ID=module.TENANT,
                   AZURE_FEDERATED_TOKEN_FILE='/fake/token', AZURE_TOKEN_CREDENTIALS='WorkloadIdentityCredential',
                   DATABASE_AUTH_MODE='workload_identity', PGUSER='trafficxx-lab-publisher',
                   PGHOST='trafficxx-pg-e8708a85.postgres.database.azure.com', PGDATABASE='trafficxx',
                   SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=module.NAMESPACE, SERVICEBUS_QUEUE=module.QUEUE)
        if mode == 'broad-chain':
            env['AZURE_TOKEN_CREDENTIALS'] = 'prod'
        if mode == 'connection-string':
            env['SERVICEBUS_CONNECTION_STRING'] = 'DO_NOT_LOG_RAW'
        output = io.StringIO()
        with patch.dict('sys.modules', {'trafficxx.broker': broker, 'trafficxx.messaging': messaging,
                                       'trafficxx.db': database}), patch.dict(os.environ, env, clear=True), \
                patch.object(module, 'Path', return_value=path), \
                patch.object(module, 'version', return_value='1.23.0' if mode == 'old-sdk' else '1.26.0'), \
                patch.object(module.logging, 'disable'), contextlib.redirect_stdout(output):
            status = module.main()
        self.assertNotIn('DO_NOT_LOG_RAW', output.getvalue())
        return status, json.loads(output.getvalue()), broker, messaging, database, connection

    def test_one_real_application_call_and_persisted_readbacks(self):
        status, result, broker, messaging, database, connection = self.scenario()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'PUBLISHER_SEND_OK')
        self.assertTrue(result['send_acknowledged'])
        self.assertTrue(result['publication_committed'])
        self.assertEqual(result['send_calls'], 1)
        broker.send_event.assert_called_once()
        messaging.publish_one.assert_called_once()
        messaging.heartbeat.assert_called_once_with('publisher')
        self.assertEqual(database.connect.call_count, 2)
        sql = [c.args[0] for c in connection.execute.call_args_list]
        self.assertEqual(sql.count('SET TRANSACTION READ ONLY'), 2)

    def test_empty_outbox_is_not_publication_success(self):
        status, result, broker, messaging, _, _ = self.scenario('idle')
        self.assertEqual(status, 1)
        self.assertEqual(result['send_calls'], 0)
        broker.send_event.assert_not_called()
        messaging.heartbeat.assert_not_called()

    def test_sent_then_commit_error_reports_unknown_commit_and_stable_id(self):
        status, result, broker, _, _, _ = self.scenario('commit-error')
        self.assertEqual(status, 1)
        self.assertTrue(result['send_acknowledged'])
        self.assertIsNone(result['publication_committed'])
        self.assertEqual(result['message_id'], 'stable-message-id')
        broker.send_event.assert_called_once()

    def test_client_close_error_preserves_confirmed_publication_evidence(self):
        status, result, _, messaging, _, _ = self.scenario('close-error')
        self.assertEqual(status, 1)
        self.assertTrue(result['publication_committed'])
        messaging.heartbeat.assert_not_called()

    def test_failed_heartbeat_does_not_hide_prior_publication(self):
        status, result, broker, _, _, _ = self.scenario('heartbeat-error')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'heartbeat')
        self.assertTrue(result['publication_committed'])
        broker.send_event.assert_called_once()

    def test_bad_readback_fails_without_sending_another_event(self):
        status, result, broker, messaging, _, _ = self.scenario('wrong-readback')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'publication_readback')
        broker.send_event.assert_called_once()
        messaging.heartbeat.assert_not_called()

    def test_second_callback_is_rejected_before_another_send(self):
        status, result, broker, _, _, _ = self.scenario('double-send')
        self.assertEqual(status, 1)
        self.assertEqual(result['send_calls'], 1)
        broker.send_event.assert_called_once()

    def test_wrong_credential_mode_connection_string_or_sdk_never_sends(self):
        for mode in ('broad-chain', 'connection-string', 'old-sdk'):
            with self.subTest(mode=mode):
                status, result, broker, messaging, _, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['check'], 'preflight')
                broker.client.assert_not_called()
                messaging.publish_one.assert_not_called()
