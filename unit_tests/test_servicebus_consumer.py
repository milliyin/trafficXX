"""Offline private-network and least-privilege diagnostic decisions."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    'servicebus_consumer_check', Path(__file__).resolve().parents[1] / 'scripts/check-servicebus-consumer.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Denied(Exception):
    pass


class ServiceBusConsumer(unittest.TestCase):
    def scenario(self, mode='success'):
        identity = types.ModuleType('azure.identity')
        credential = MagicMock()
        credential.__enter__.return_value = credential
        identity.WorkloadIdentityCredential = MagicMock(return_value=credential)
        servicebus = types.ModuleType('azure.servicebus')
        servicebus.ServiceBusSubQueue = types.SimpleNamespace(DEAD_LETTER='dead-letter')
        bus = MagicMock()
        bus.__enter__.return_value = bus
        servicebus.ServiceBusClient = MagicMock(return_value=bus)
        sender, receiver = MagicMock(), MagicMock()
        sender.__enter__.return_value = sender
        receiver.__enter__.return_value = receiver
        bus.get_queue_sender.return_value = sender
        bus.get_queue_receiver.return_value = receiver
        if mode == 'sender-batch-denied':
            sender.create_message_batch.side_effect = Denied('DO_NOT_LOG_RAW')
        elif mode == 'sender-timeout':
            sender.__enter__.side_effect = TimeoutError('DO_NOT_LOG_RAW')
        elif mode != 'send-allowed':
            sender.__enter__.side_effect = Denied('DO_NOT_LOG_RAW')
        if mode == 'receiver-denied':
            receiver.__enter__.side_effect = Denied('DO_NOT_LOG_RAW')
        elif mode == 'receive-timeout':
            receiver.peek_messages.side_effect = TimeoutError('DO_NOT_LOG_RAW')
        elif mode == 'dead-letter-denied':
            receiver.peek_messages.side_effect = [[], Denied('DO_NOT_LOG_RAW')]
        errors = types.ModuleType('azure.servicebus.exceptions')
        errors.ServiceBusAuthorizationError = Denied
        claims = {'sub': 'wrong' if mode == 'wrong-subject' else 'system:serviceaccount:trafficxx:consumer'}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        path = MagicMock()
        path.read_text.return_value = 'header.' + payload + '.signature'
        path.is_file.return_value = mode != 'missing-ca'
        raw = MagicMock()
        raw.__enter__.return_value = raw
        tls = MagicMock()
        tls.__enter__.return_value = tls
        tls.getpeername.return_value = ('20.1.2.3' if mode == 'public-peer' else '10.40.17.4', 5671)
        tls.version.return_value = 'TLSv1.3'
        context = MagicMock()
        context.wrap_socket.return_value = tls
        env = dict(AZURE_CLIENT_ID='wrong' if mode == 'wrong-client' else module.CLIENT,
                   AZURE_TENANT_ID=module.TENANT, AZURE_FEDERATED_TOKEN_FILE='/fake/token',
                   SERVICEBUS_FULLY_QUALIFIED_NAMESPACE=module.NAMESPACE, SERVICEBUS_QUEUE=module.QUEUE)
        if mode == 'connection-string':
            env['SERVICEBUS_CONNECTION_STRING'] = 'DO_NOT_LOG_RAW'
        address = '20.1.2.3' if mode == 'public-dns' else '10.40.17.4'
        output = io.StringIO()
        module.CURRENT_CHECK = 'identity_preflight'
        with patch.dict('sys.modules', {'azure': types.ModuleType('azure'), 'azure.identity': identity,
                                       'azure.servicebus': servicebus, 'azure.servicebus.exceptions': errors}), \
                patch.dict(os.environ, env, clear=True), patch.object(module, 'Path', return_value=path), \
                patch.object(module.socket, 'getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 5671))]) as dns, \
                patch.object(module.socket, 'create_connection', return_value=raw), \
                patch.object(module.ssl, 'create_default_context', return_value=context), \
                patch.object(module.logging, 'disable'), contextlib.redirect_stdout(output):
            status = module.main()
        if status == 0:
            self.assertEqual(bus.get_queue_receiver.call_count, 2)
            calls = bus.get_queue_receiver.call_args_list
            self.assertTrue(all(call.kwargs['prefetch_count'] == 0 for call in calls))
            self.assertNotIn('sub_queue', calls[0].kwargs)
            self.assertEqual(calls[1].kwargs['sub_queue'], servicebus.ServiceBusSubQueue.DEAD_LETTER)
        self.assertNotIn('DO_NOT_LOG_RAW', output.getvalue())
        return status, json.loads(output.getvalue()), identity, sender, receiver, dns, context

    def test_success_peeks_both_queues_without_receive_send_or_settlement(self):
        status, result, _, sender, receiver, _, context = self.scenario()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'SERVICEBUS_CONSUMER_ACCESS_OK')
        self.assertEqual(result['private_ips'], ['10.40.17.4'])
        self.assertTrue(result['send_denied'])
        self.assertTrue(result['dead_letter_peek_allowed'])
        self.assertEqual(result['broker_messages_received'], 0)
        sender.send_messages.assert_not_called()
        receiver.receive_messages.assert_not_called()
        receiver.complete_message.assert_not_called()
        receiver.abandon_message.assert_not_called()
        receiver.dead_letter_message.assert_not_called()
        self.assertEqual(receiver.peek_messages.call_count, 2)
        self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'], module.NAMESPACE)

    def test_wrong_identity_or_sas_config_stops_before_network(self):
        for mode in ('wrong-client', 'wrong-subject', 'connection-string', 'missing-ca'):
            with self.subTest(mode=mode):
                status, result, identity, _, _, dns, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['check'], 'identity_preflight')
                identity.WorkloadIdentityCredential.assert_not_called()
                dns.assert_not_called()

    def test_public_dns_cannot_pass_as_private_connectivity(self):
        status, result, identity, _, _, _, _ = self.scenario('public-dns')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'private_dns')
        identity.WorkloadIdentityCredential.assert_not_called()

    def test_public_tls_peer_is_rejected_even_with_private_dns_result(self):
        status, result, identity, _, _, _, _ = self.scenario('public-peer')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'amqp_tls')
        identity.WorkloadIdentityCredential.assert_not_called()

    def test_queue_or_dead_letter_denial_fails_before_sender(self):
        for mode, stage in [('receiver-denied', 'receiver_link'), ('dead-letter-denied', 'dead_letter_peek')]:
            with self.subTest(mode=mode):
                status, result, _, sender, _, _, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['check'], stage)
                sender.__enter__.assert_not_called()

    def test_batch_authorization_error_is_valid_send_denial(self):
        self.assertEqual(self.scenario('sender-batch-denied')[0], 0)

    def test_unexpected_send_link_access_is_failure_without_sending(self):
        status, result, _, sender, _, _, _ = self.scenario('send-allowed')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'send_denial')
        self.assertEqual(result['error_type'], 'ValueError')
        sender.send_messages.assert_not_called()

    def test_timeout_does_not_count_as_permission_denial(self):
        for mode in ('receive-timeout', 'sender-timeout'):
            with self.subTest(mode=mode):
                status, result, _, _, _, _, _ = self.scenario(mode)
                self.assertEqual(status, 1)
                self.assertEqual(result['error_type'], 'TimeoutError')
