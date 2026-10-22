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
    'servicebus_publisher_check', Path(__file__).resolve().parents[1] / 'scripts/check-servicebus-publisher.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Denied(Exception):
    pass


class ServiceBusPublisher(unittest.TestCase):
    def scenario(self, mode='success'):
        identity = types.ModuleType('azure.identity')
        credential = MagicMock()
        credential.__enter__.return_value = credential
        identity.WorkloadIdentityCredential = MagicMock(return_value=credential)
        servicebus = types.ModuleType('azure.servicebus')
        bus = MagicMock()
        bus.__enter__.return_value = bus
        servicebus.ServiceBusClient = MagicMock(return_value=bus)
        sender, receiver = MagicMock(), MagicMock()
        sender.__enter__.return_value = sender
        receiver.__enter__.return_value = receiver
        bus.get_queue_sender.return_value = sender
        bus.get_queue_receiver.return_value = receiver
        if mode == 'sender-denied':
            sender.__enter__.side_effect = Denied('DO_NOT_LOG_RAW')
        if mode == 'receiver-enter-denied':
            receiver.__enter__.side_effect = Denied('DO_NOT_LOG_RAW')
        elif mode == 'receive-timeout':
            receiver.peek_messages.side_effect = TimeoutError('DO_NOT_LOG_RAW')
        elif mode != 'receive-allowed':
            receiver.peek_messages.side_effect = Denied('DO_NOT_LOG_RAW')
        errors = types.ModuleType('azure.servicebus.exceptions')
        errors.ServiceBusAuthorizationError = Denied
        claims = {'sub': 'wrong' if mode == 'wrong-subject' else 'system:serviceaccount:trafficxx:publisher'}
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
        self.assertNotIn('DO_NOT_LOG_RAW', output.getvalue())
        return status, json.loads(output.getvalue()), identity, sender, receiver, dns, context

    def test_success_opens_sender_but_never_sends_or_receives_messages(self):
        status, result, _, sender, receiver, _, context = self.scenario()
        self.assertEqual(status, 0)
        self.assertEqual(result['result'], 'SERVICEBUS_PUBLISHER_ACCESS_OK')
        self.assertEqual(result['private_ips'], ['10.40.17.4'])
        self.assertTrue(result['receive_denied'])
        self.assertEqual(result['broker_messages_sent'], 0)
        sender.create_message_batch.assert_called_once_with()
        sender.send_messages.assert_not_called()
        receiver.receive_messages.assert_not_called()
        receiver.peek_messages.assert_called_once_with(max_message_count=1, timeout=10)
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

    def test_sender_denial_is_a_failure_not_receive_denial_evidence(self):
        status, result, _, _, receiver, _, _ = self.scenario('sender-denied')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'sender_link')
        receiver.peek_messages.assert_not_called()

    def test_receiver_link_authorization_error_is_valid_denial(self):
        self.assertEqual(self.scenario('receiver-enter-denied')[0], 0)

    def test_unexpected_receive_access_is_failure(self):
        status, result, _, _, _, _, _ = self.scenario('receive-allowed')
        self.assertEqual(status, 1)
        self.assertEqual(result['check'], 'receive_denial')
        self.assertEqual(result['error_type'], 'ValueError')

    def test_timeout_does_not_count_as_permission_denial(self):
        status, result, _, _, _, _, _ = self.scenario('receive-timeout')
        self.assertEqual(status, 1)
        self.assertEqual(result['error_type'], 'TimeoutError')
