"""Cloud CLI orchestration safeguards; no actual cloud access."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'consumer_backlog', Path(__file__).resolve().parents[1] / 'scripts/check-consumer-backlog.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Cloud:
    def __init__(self, mode='success'):
        self.mode, self.calls = mode, []
        self.replicas, self.created, self.queue_calls = 1, False, 0

    def __call__(self, args, payload=None):
        self.calls.append(args)
        if args[:3] == ['az', 'account', 'show']:
            return 'wrong' if self.mode == 'wrong-subscription' else module.SUBSCRIPTION
        if args[:3] == ['kubectl', 'config', 'current-context']:
            return 'trafficxx-lab-aks'
        if args[:3] == ['kubectl', 'get', 'deployment/consumer']:
            return json.dumps({'metadata': {'uid': 'original'},
                               'spec': {'replicas': self.replicas, 'selector': {'matchLabels': {
                                   'app.kubernetes.io/name': 'trafficxx-consumer'}}},
                               'status': {'readyReplicas': 1}})
        if args[:3] == ['az', 'servicebus', 'queue']:
            self.queue_calls += 1
            count = 2 if self.created and self.replicas == 0 else 0
            if self.mode == 'backlog-timeout' and self.created:
                count = 1
            if self.mode == 'dirty-start' and not self.created:
                count = 1
            return json.dumps({'active': count, 'dead_letters': 0, 'total': count})
        if args[:2] == ['kubectl', 'scale']:
            if '--replicas=0' in args:
                self.replicas = 0
                if self.mode == 'ambiguous-scale':
                    raise RuntimeError('Scale response lost')
            else:
                self.replicas = 1
            return ''
        if args[:3] == ['kubectl', 'get', 'pods']:
            return '{"items": []}'
        if args[:3] == ['kubectl', 'get', 'configmap/consumer-drain-check-files']:
            return 'configmap/consumer-drain-check-files'
        if args[0] == 'bash':
            self.created = True
            if self.mode == 'booking-failure':
                raise RuntimeError('Booking failure')
            if self.mode == 'interrupt':
                raise KeyboardInterrupt()
            return json.dumps({'result': 'BOOKING_FLOW_OK', 'final_status': 'CANCELLED', 'reservation_id': 'fixture'})
        if args[:2] == ['kubectl', 'create']:
            return json.dumps({'metadata': {'name': 'consumer-drain-check'}})
        if args[:2] == ['kubectl', 'logs']:
            return json.dumps({'result': 'FAILED' if self.mode == 'observer-failure' else 'CONSUMER_DRAIN_OK',
                               'read_only': True})
        if args[:2] == ['kubectl', 'apply']:
            job = json.loads(payload)
            assert job['metadata']['name'].startswith('backlog-drain-')
            return ''
        if args[0] == 'kubectl' and args[1] in ('rollout', 'wait', 'delete'):
            return ''
        raise AssertionError(args)


class ConsumerBacklog(unittest.TestCase):
    def test_success_observes_two_waiting_events_then_restores_and_checks_sql(self):
        cloud = Cloud()
        result = module.run(cloud, lambda _: None)
        self.assertEqual(result['result'], 'CONSUMER_BACKLOG_RECOVERY_OK')
        self.assertEqual(result['queue_while_paused']['active'], 2)
        self.assertEqual(result['queue_after']['total'], 0)
        self.assertEqual(cloud.replicas, 1)
        self.assertTrue(any(c[:2] == ['kubectl', 'delete'] for c in cloud.calls))

    def test_preflight_failures_do_not_scale_or_create_bookings(self):
        for mode in ('wrong-subscription', 'dirty-start'):
            with self.subTest(mode=mode):
                cloud = Cloud(mode)
                with self.assertRaises(ValueError):
                    module.run(cloud, lambda _: None)
                self.assertFalse(any(c[:2] == ['kubectl', 'scale'] or c[0] == 'bash' for c in cloud.calls))

    def test_failed_booking_still_restores_consumer(self):
        cloud = Cloud('booking-failure')
        with self.assertRaises(RuntimeError):
            module.run(cloud, lambda _: None)
        self.assertEqual(cloud.replicas, 1)

    def test_keyboard_interrupt_still_restores_consumer(self):
        cloud = Cloud('interrupt')
        with self.assertRaises(KeyboardInterrupt):
            module.run(cloud, lambda _: None)
        self.assertEqual(cloud.replicas, 1)

    def test_ambiguous_pause_still_restores_consumer(self):
        cloud = Cloud('ambiguous-scale')
        with self.assertRaises(RuntimeError):
            module.run(cloud, lambda _: None)
        self.assertEqual(cloud.replicas, 1)

    def test_backlog_timeout_is_bounded_and_restores_consumer(self):
        cloud = Cloud('backlog-timeout')
        with self.assertRaises(TimeoutError):
            module.run(cloud, lambda _: None)
        self.assertEqual(cloud.replicas, 1)
        self.assertEqual(cloud.queue_calls, 25)

    def test_failed_sql_evidence_cannot_pass_and_observer_job_is_retained(self):
        cloud = Cloud('observer-failure')
        with self.assertRaises(ValueError):
            module.run(cloud, lambda _: None)
        self.assertEqual(cloud.replicas, 1)
        self.assertFalse(any(c[:2] == ['kubectl', 'delete'] for c in cloud.calls))
        self.assertEqual(module.CURRENT_CHECK, 'validate_database_observer')
        self.assertTrue(module.CURRENT_OBSERVER_JOB.startswith('backlog-drain-'))

    def test_wait_command_failure_preserves_job_and_restored_consumer(self):
        cloud = Cloud()

        def fail_wait(args, payload=None):
            if args[:2] == ['kubectl', 'wait']:
                raise module.CommandFailed(1)
            return cloud(args, payload)
        with self.assertRaises(module.CommandFailed) as error:
            module.run(fail_wait, lambda _: None)
        self.assertEqual(error.exception.command_exit_code, 1)
        self.assertEqual(module.CURRENT_CHECK, 'wait_database_observer')
        self.assertEqual(cloud.replicas, 1)
        self.assertFalse(any(c[:2] == ['kubectl', 'delete'] for c in cloud.calls))

    def test_changed_deployment_is_not_overwritten_during_restore(self):
        def changed(args, payload=None):
            return json.dumps({'metadata': {'uid': 'other'}, 'spec': {'replicas': 0}})
        with self.assertRaises(ValueError):
            module.restore(changed, 'original')
