"""Expiry-boundary decisions use fake time; no real credentials or long waits."""
import importlib.util
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    'token_renewal', Path(__file__).resolve().parents[1] / 'scripts/check-token-renewal.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Clock:
    def __init__(self):
        self.value = 1000
        self.elapsed = 0

    def now(self):
        return self.value

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.value += seconds
        self.elapsed += seconds


class TokenRenewal(unittest.TestCase):
    def scenario(self, fault=None):
        clock = Clock()
        records = []

        def probe():
            refreshed = clock.now() >= 1500
            return {'expires_on': (100000 if fault == 'too-long' else 5200 if refreshed else 1600),
                    'observed_at': 1000 if fault == 'old-session' else clock.now(),
                    'token_changed': refreshed and fault != 'unchanged-token',
                    'server_epoch': 1629 if fault == 'server-before-boundary' and clock.now() >= 1630 else clock.now(),
                    'database': 'trafficxx', 'identity': 'trafficxx-lab-browse',
                    'ssl': True, 'tls_version': 'TLSv1.3'}

        def run():
            return module.exercise(probe, records.append, clock=clock.now,
                                   monotonic=clock.monotonic, sleep=clock.sleep)

        return clock, records, run

    def test_requires_real_expiry_boundary_new_login_and_changed_token(self):
        clock, records, run = self.scenario()
        result = run()
        self.assertEqual(result['result'], 'DATABASE_TOKEN_RENEWAL_OK')
        self.assertEqual(clock.now(), 1630)
        self.assertEqual(result['elapsed_seconds'], 630)
        self.assertEqual(result['sql_checks'], 4)
        self.assertEqual(records[0]['result'], 'TOKEN_RENEWAL_WAITING')
        self.assertEqual({row['run_id'] for row in records}, {result['run_id']})
        self.assertTrue(result['new_sql_login_after_initial_expiry'])
        self.assertFalse(result['inventory_writes'])

    def test_long_lifetime_fails_before_waiting_outside_budget(self):
        clock, _, run = self.scenario('too-long')
        with self.assertRaisesRegex(module.RenewalFailure, 'EXPIRY_OUTSIDE'):
            run()
        self.assertEqual(clock.elapsed, 0)

    def test_existing_session_or_unchanged_token_cannot_prove_renewal(self):
        for fault in ('old-session', 'unchanged-token', 'server-before-boundary'):
            with self.subTest(fault=fault):
                _, records, run = self.scenario(fault)
                with self.assertRaises(module.RenewalFailure):
                    run()
                self.assertNotIn('DATABASE_TOKEN_RENEWAL_OK', [row['result'] for row in records])

    def test_initial_expired_token_and_clock_skew_are_rejected(self):
        for expiry, server_epoch in ((999, 1000), (2000, 800)):
            with self.subTest(expiry=expiry), self.assertRaises(module.RenewalFailure):
                module.exercise(lambda: {'expires_on': expiry, 'server_epoch': server_epoch},
                                lambda row: self.fail('must not emit success'), clock=lambda: 1000,
                                monotonic=lambda: 0, sleep=lambda seconds: self.fail('must not sleep'))

    def test_monotonic_deadline_handles_wall_clock_not_advancing(self):
        clock, _, _ = self.scenario()
        def sleep(seconds):
            clock.elapsed = 90001
        with self.assertRaisesRegex(module.RenewalFailure, 'DEADLINE'):
            module.exercise(lambda: {'expires_on': 1600, 'server_epoch': 1000}, lambda row: None,
                            clock=clock.now, monotonic=clock.monotonic, sleep=sleep)

    def test_sdk_observer_preserves_real_calls_without_token_or_digest_output(self):
        real = MagicMock()
        tokens = [SimpleNamespace(token='PRIVATE_FIRST_TOKEN', expires_on=1600),
                  SimpleNamespace(token='PRIVATE_NEW_TOKEN', expires_on=5200)]
        real.get_token.side_effect = tokens
        observed = module.ObservedCredential(real, clock=lambda: 1000)
        self.assertIs(observed.get_token('scope'), tokens[0])
        self.assertFalse(observed.metadata()['token_changed'])
        self.assertIs(observed.get_token('scope'), tokens[1])
        self.assertTrue(observed.metadata()['token_changed'])
        text = json.dumps(observed.metadata())
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('digest', text)
        self.assertEqual(real.get_token.call_count, 2)
        real.get_token.assert_called_with('scope')

    def test_main_failure_does_not_print_raw_exception(self):
        output = io.StringIO()
        with patch.object(module, 'real_probe', side_effect=RuntimeError('PRIVATE_RAW_ERROR')), redirect_stdout(output):
            self.assertEqual(module.main(), 1)
        row = json.loads(output.getvalue())
        self.assertEqual(row['result'], 'DATABASE_TOKEN_RENEWAL_FAILED')
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cleanup_failure_cannot_print_a_success_result(self):
        output = io.StringIO()
        database = MagicMock()
        database.close.side_effect = RuntimeError('PRIVATE_CLOSE_ERROR')
        with patch.object(module, 'real_probe', return_value=(database, MagicMock())), \
                patch.object(module, 'exercise', return_value={'result': 'DATABASE_TOKEN_RENEWAL_OK'}), \
                redirect_stdout(output):
            self.assertEqual(module.main(), 1)
        self.assertNotIn('DATABASE_TOKEN_RENEWAL_OK', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())
