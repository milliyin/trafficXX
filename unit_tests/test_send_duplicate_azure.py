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
    'send_duplicate_azure', Path(__file__).resolve().parents[1] / 'scripts/send-duplicate-azure.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SendDuplicateAzure(unittest.TestCase):
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

        event = types.SimpleNamespace(outbox_id=3,
            message_id='151af373-f74d-4bc4-8c14-e3b6cf13aa51', status='HELD')
        messaging.envelope = MagicMock(return_value=event)
        if mode == 'send-timeout':
            broker.send_event.side_effect = TimeoutError('DO_NOT_LOG_RAW')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        database.connect = MagicMock(return_value=connection)

        def execute(sql, params=None):
            cursor = MagicMock()
            if 'FROM public.outbox' in sql:
                cursor.fetchone.return_value = None if mode == 'missing' else dict(
                    id=3, message_id='wrong' if mode == 'wrong-id' else event.message_id,
                    published_at=None if mode == 'unpublished' else 'timestamp')
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

    def test_resends_original_event_once_without_sql_writes(self):
        status, result, broker, messaging, database, connection = self.scenario()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'DUPLICATE_SEND_OK')
        self.assertEqual(result['send_calls'], 1)
        self.assertTrue(result['send_acknowledged'])
        self.assertFalse(result['outbox_edited'])
        broker.send_event.assert_called_once()
        self.assertEqual(broker.send_event.call_args.args[1].outbox_id, 3)
        sql = [c.args[0] for c in connection.execute.call_args_list]
        self.assertEqual(sql, ['SET TRANSACTION READ ONLY', 'SELECT * FROM public.outbox WHERE id = %s'])
        self.assertEqual(database.connect.call_count, 1)
        messaging.envelope.assert_called_once()

    def test_missing_unpublished_or_wrong_identity_never_sends(self):
        for mode in ('missing', 'unpublished', 'wrong-id'):
            with self.subTest(mode=mode):
                status, result, broker, _, _, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['send_calls'], 0)
                broker.send_event.assert_not_called()

    def test_send_timeout_reports_attempt_without_ack_or_retry(self):
        status, result, broker, _, _, _ = self.scenario('send-timeout')
        self.assertEqual(status, 1)
        self.assertEqual(result['send_calls'], 1)
        self.assertFalse(result['send_acknowledged'])
        self.assertEqual(result['message_id'], '151af373-f74d-4bc4-8c14-e3b6cf13aa51')
        broker.send_event.assert_called_once()

    def test_close_failure_preserves_acknowledged_send(self):
        status, result, broker, _, _, _ = self.scenario('close-error')
        self.assertEqual(status, 1)
        self.assertTrue(result['send_acknowledged'])
        broker.send_event.assert_called_once()

    def test_credential_mode_sas_or_sdk_mismatch_fails_before_broker(self):
        for mode in ('broad-chain', 'connection-string', 'old-sdk'):
            with self.subTest(mode=mode):
                status, result, broker, _, database, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['check'], 'preflight')
                broker.client.assert_not_called()
                database.connect.assert_not_called()
