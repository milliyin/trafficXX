"""Typed network evidence without raw messages, retries or timeout changes."""
from contextlib import ExitStack, redirect_stdout
import errno
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


read = load('transport_read', 'check-read-load.py')
with patch.dict(sys.modules, {'read_probe': read}):
    probe = load('transport_probe', 'check-browse-autoscaling-load.py')
queue = load('transport_queue', 'browse-queue-comparison.py')


class Transport(unittest.TestCase):
    def test_typed_reasons_are_allowlisted_and_redacted(self):
        cases = [(socket.gaierror(socket.EAI_AGAIN, 'PRIVATE'), 'dns_temporary_failure'),
                 (socket.gaierror(socket.EAI_NONAME, 'PRIVATE'), 'dns_resolution_failure'),
                 (TimeoutError('PRIVATE'), 'timeout'),
                 (OSError(errno.ECONNREFUSED, 'PRIVATE'), 'connection_refused'),
                 (OSError(errno.ECONNRESET, 'PRIVATE'), 'connection_reset'),
                 (OSError(errno.ENETUNREACH, 'PRIVATE'), 'network_unreachable'),
                 (OSError(errno.EMFILE, 'PRIVATE'), 'client_resource_exhausted'),
                 (OSError(errno.EADDRNOTAVAIL, 'PRIVATE'), 'client_address_unavailable'),
                 (http.client.RemoteDisconnected('PRIVATE'), 'remote_disconnected'),
                 (http.client.IncompleteRead(b'PRIVATE'), 'incomplete_response'),
                 (http.client.BadStatusLine('PRIVATE'), 'http_protocol_error')]
        for reason, category in cases:
            for error in (reason, urllib.error.URLError(reason)):
                with self.subTest(category=category, wrapped=error is not reason):
                    row = read.transport_failure(error)
                    self.assertEqual(row['category'], category)
                    self.assertEqual(row['operation'], 'unknown')
                    self.assertNotIn('PRIVATE', json.dumps(row))

    def test_unknown_string_reason_is_not_parsed_and_cycles_are_bounded(self):
        row = read.transport_failure(urllib.error.URLError('PRIVATE timeout host-name'))
        self.assertEqual(row['category'], 'unknown_transport_error')
        self.assertNotIn('PRIVATE', json.dumps(row))
        error = urllib.error.URLError('PRIVATE')
        error.reason = error
        error.trafficxx_operation = 'PRIVATE'
        error.trafficxx_http_status = 'PRIVATE'
        row = read.transport_failure(error)
        self.assertEqual(row['operation'], 'unknown')
        self.assertIsNone(row['received_http_status'])

    def test_http_and_validation_errors_are_not_classified_as_transport(self):
        error = urllib.error.HTTPError('http://PRIVATE', 503, 'PRIVATE', {}, None)
        self.assertIsNone(read.transport_failure(error))
        self.assertIsNone(read.transport_failure(read.CheckFailed('REQUEST_OVER_TWO_SECONDS')))
        self.assertIsNone(read.transport_failure(ValueError('PRIVATE')))

    def test_open_error_is_rethrown_without_retry_and_does_not_claim_connect_only(self):
        error = urllib.error.URLError(TimeoutError('PRIVATE'))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(read, 'http_opener', return_value=opener):
            with self.assertRaises(urllib.error.URLError) as caught:
                read.read_url('http://browse.trafficxx.svc.cluster.local/events', 'PRIVATE')
        self.assertIs(caught.exception, error)
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 5})
        row = read.transport_failure(error)
        self.assertEqual((row['category'], row['operation']), ('timeout', 'open_or_headers'))
        self.assertIn('does not isolate', row['note'])

    def test_body_failure_records_received_status_and_closes_response(self):
        opener = MagicMock()
        response = opener.open.return_value
        response.code = 200
        error = http.client.IncompleteRead(b'PRIVATE')
        response.read.side_effect = error
        with patch.object(read, 'http_opener', return_value=opener):
            with self.assertRaises(http.client.IncompleteRead):
                read.read_url('http://browse.trafficxx.svc.cluster.local/events', 'PRIVATE')
        row = read.transport_failure(error)
        self.assertEqual((row['category'], row['operation'], row['received_http_status']),
                         ('incomplete_response', 'read_body', 200))
        self.assertNotIn('PRIVATE', json.dumps(row))
        response.__exit__.assert_called_once()

    def test_first_failure_and_retained_summary_contain_typed_reason(self):
        error = urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, 'PRIVATE'))
        error.trafficxx_operation = 'open_or_headers'
        with patch.object(probe, 'publish') as publish, redirect_stdout(io.StringIO()):
            row = probe.run('a' * 12, users=1000, duration=420, ramp_down=True,
                            http=Mock(side_effect=error))
        self.assertEqual(row['result'], 'BROWSE_SCALING_LOAD_FAILED')
        self.assertGreaterEqual(row['errors']['URLError'], 1)
        self.assertEqual(row['first_failure']['transport']['category'], 'dns_temporary_failure')
        self.assertEqual(row['first_failure']['transport']['errno_name'], 'EAI_AGAIN')
        self.assertEqual(row['transport_diagnostics_policy'], read.TRANSPORT_DIAGNOSTICS_POLICY)
        self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertEqual(publish.call_args.args, ('summary.json', row))

    def test_network_cli_uses_distinct_job_and_existing_image(self):
        with patch.object(queue, 'run') as run, patch.object(queue.s, 'DURABLE_EVIDENCE', False), \
             patch.object(queue.s, 'WAIT_TIME_MEASUREMENT', False), patch.object(queue.s, 'QUEUE32_PROFILE_JOB', 'old'), \
             patch.object(queue.argparse.ArgumentParser, 'parse_args',
                          return_value=SimpleNamespace(action='run-network-measured')):
            self.assertEqual(queue.main(), 0)
            run.assert_called_once_with(reuse_image=True)
            self.assertTrue(queue.s.DURABLE_EVIDENCE)
            self.assertTrue(queue.s.WAIT_TIME_MEASUREMENT)
            self.assertEqual(queue.s.QUEUE32_PROFILE_JOB, 'browse-users-1000-queue32-network')

    def test_reuse_image_still_checks_baseline_and_restores_after_load(self):
        s, events = queue.s, []
        def command(args, **kwargs):
            self.assertEqual(args[0], 'python3')
            self.assertIn('--require-health-check', args)
            events.append('baseline')
            return json.dumps({'result': 'BROWSE_TIMING_BASELINE_OK'})
        with ExitStack() as stack:
            for name in ('context', 'no_active_load', 'health'):
                stack.enter_context(patch.object(s.trial, name))
            stack.enter_context(patch.object(s.trial, 'deployment', return_value={'metadata': {}}))
            stack.enter_context(patch.object(s.base, 'kube', side_effect=lambda *a: '' if not events else 'job/owned'))
            stack.enter_context(patch.object(s.base, 'command', side_effect=command))
            deploy = stack.enter_context(patch.object(s, 'deploy_image'))
            stack.enter_context(patch.object(queue, 'patch_queue', side_effect=lambda *a, **kw: events.append('queue32')))
            stack.enter_context(patch.object(s, 'drill_prewarmed', side_effect=lambda **kw: events.append('load')))
            stack.enter_context(patch.object(s, 'diagnose_500', side_effect=lambda **kw: events.append('report')))
            stack.enter_context(patch.object(queue, 'restore', side_effect=lambda **kw: events.append('restore')))
            stack.enter_context(redirect_stdout(io.StringIO()))
            queue.run(reuse_image=True)
            deploy.assert_not_called()
        self.assertEqual(events, ['queue32', 'baseline', 'load', 'report', 'restore'])


if __name__ == '__main__':
    unittest.main()
