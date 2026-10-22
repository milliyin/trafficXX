"""Diagnostic send/peek/targeted cleanup decisions with fake broker objects."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    'poison_message', Path(__file__).resolve().parents[1] / 'scripts/poison-message-azure.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def message(**changes):
    return SimpleNamespace(**{**dict(message_id=module.MESSAGE_ID, dead_letter_reason=module.REASON,
                                    sequence_number=77, body=[module.BODY.encode()]), **changes})


class PoisonMessage(unittest.TestCase):
    def setUp(self):
        module.STATE.clear()
        self.bus, self.sender, self.receiver = MagicMock(), MagicMock(), MagicMock()
        self.bus.get_queue_sender.return_value.__enter__.return_value = self.sender
        self.bus.get_queue_receiver.return_value.__enter__.return_value = self.receiver
        self.receiver.peek_messages.return_value = [message()]
        self.receiver.receive_messages.return_value = [message()]
        self.kind = MagicMock()

    def run_action(self, action):
        return module.operate(self.bus, action, self.kind, 'dead-letter', lambda _: None)

    def test_sends_one_identifiable_invalid_message(self):
        result = self.run_action('send')
        self.assertEqual(result['result'], 'POISON_SEND_OK')
        self.sender.send_messages.assert_called_once()
        self.assertEqual(self.kind.call_args.kwargs['message_id'], module.MESSAGE_ID)
        self.assertIn('999', self.kind.call_args.args[0])
        self.bus.get_queue_receiver.assert_not_called()

    def test_peek_verifies_reason_and_identity_without_receiving(self):
        result = self.run_action('peek')
        self.assertEqual(result['result'], 'POISON_DLQ_PEEK_OK')
        self.assertFalse(result['settled'])
        self.assertEqual(self.bus.get_queue_receiver.call_args.kwargs['prefetch_count'], 0)
        self.receiver.receive_messages.assert_not_called()
        self.receiver.complete_message.assert_not_called()

    def test_cleanup_only_completes_verified_matching_message(self):
        result = self.run_action('cleanup')
        self.assertEqual(result['result'], 'POISON_CLEANUP_OK')
        self.assertTrue(result['completion_acknowledged'])
        self.receiver.complete_message.assert_called_once()
        self.receiver.abandon_message.assert_not_called()

    def test_different_peeked_message_reason_body_or_multiple_messages_blocks_cleanup(self):
        for msgs in ([message(message_id='other')], [message(dead_letter_reason='OTHER')],
                     [message(body=[b'other'])], [message(), message()]):
            with self.subTest(msgs=msgs):
                self.receiver.peek_messages.return_value = msgs
                with self.assertRaises(ValueError):
                    self.run_action('cleanup')
                self.receiver.receive_messages.assert_not_called()
                self.receiver.complete_message.assert_not_called()

    def test_unexpected_received_message_is_abandoned_and_never_completed(self):
        self.receiver.receive_messages.return_value = [message(sequence_number=88)]
        with self.assertRaises(ValueError):
            self.run_action('cleanup')
        self.receiver.abandon_message.assert_called_once()
        self.receiver.complete_message.assert_not_called()

    def test_missing_dead_letter_times_out_without_receiving(self):
        self.receiver.peek_messages.return_value = []
        with self.assertRaises(TimeoutError):
            self.run_action('peek')
        self.assertEqual(self.receiver.peek_messages.call_count, 12)
        self.receiver.receive_messages.assert_not_called()

    def test_ambiguous_completion_is_not_reported_as_acknowledged(self):
        self.receiver.complete_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.run_action('cleanup')
        self.assertTrue(module.STATE['completion_attempted'])
        self.assertNotIn('completion_acknowledged', module.STATE)

    def test_missing_action_or_wrong_identity_stops_before_sdk_import(self):
        for env in ({}, {'POISON_ACTION': 'cleanup', 'AZURE_CLIENT_ID': 'wrong'}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ValueError):
                    module.run()
