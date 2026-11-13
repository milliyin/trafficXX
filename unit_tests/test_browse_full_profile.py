"""Explicit bounded-queue profile: observing depth cannot hide HTTP or metric failures."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load('full_scaling', 'browse-autoscaling.py')
q = load('full_queue', 'browse-queue-comparison.py')
read = load('full_read', 'check-read-load.py')
with patch.dict(sys.modules, {'read_probe': read}):
    probe = load('full_probe', 'check-browse-autoscaling-load.py')
RUN = 'a' * 12


def rows(stamp, value):
    return [{'metric': {'instance': instance}, 'values': [[stamp, str(value)]]} for instance in ('a', 'b')]


def completed():
    return dict(result='BROWSE_SCALING_LOAD_OK', run_id=RUN, users=1000, users_with_requests=1000,
        elapsed_seconds=420, scheduled_duration_seconds=420, start_ramp_seconds=120, think_seconds=2,
        max_workload_attempts=210000, attempts=3000, status_counts={'200': 3000}, errors={},
        stop_requested=False, first_failure=None, queue_stop_policy=s.FULL_PROFILE_POLICY,
        latency_ms={'max': 233}, hold_seconds=180, ramp_down_seconds=120,
        peak_active_users=1000, active_users_after_workload=0,
        phases={p: {'attempts': 1000, 'status_counts': {'200': 1000}} for p in ('ramp_up', 'hold', 'ramp_down')},
        progress_samples=[{'elapsed_seconds': t, 'active_users': users} for t, users in
                          ((0, 1), (120, 1000), (294, 1000), (300, 1000), (360, 500), (420, 0))])


class FullProfile(unittest.TestCase):
    def test_new_mode_records_repeated_positive_depth_while_legacy_still_stops(self):
        old, new = s.PoolWaitingWatch(), s.PoolWaitingWatch(stop_on_depth=False)
        old.observe(rows(100, 1), 101)
        new.observe(rows(100, 1), 101)
        with self.assertRaisesRegex(ValueError, 'SUSTAINED_POOL_WAITING'):
            old.observe(rows(115, 1), 116)
        new.observe(rows(115, 1), 116)
        new.observe(rows(130, 5), 131)
        self.assertEqual(new.summary()['a']['distinct_source_samples'], 3)
        self.assertEqual(new.summary()['a']['sampled_peak_waiting'], 5)

    def test_observation_mode_keeps_missing_stale_changed_and_gap_guards(self):
        for data, now in (([], 100), (rows(100, 0), 146), (rows(100, -1), 101)):
            with self.assertRaises(ValueError):
                s.PoolWaitingWatch(stop_on_depth=False).observe(data, now)
        new = s.PoolWaitingWatch(stop_on_depth=False)
        new.observe(rows(100, 1), 101)
        with self.assertRaisesRegex(ValueError, 'SAMPLE_CHANGED'):
            new.observe(rows(100, 0), 102)
        with self.assertRaisesRegex(ValueError, 'SAMPLE_GAP'):
            new.observe(rows(135, 1), 136)

    def test_pass_requires_full_hold_decline_exact_policy_and_under_two_second_max(self):
        with patch.object(s, 'QUEUE_DEPTH_OBSERVATION_ONLY', True):
            s.validate_load(completed(), RUN, 1000, True, duration=420)
            for change in (lambda r: r.update(elapsed_seconds=244),
                           lambda r: r.update(stop_requested=True),
                           lambda r: r.update(queue_stop_policy='depth-10s-v1'),
                           lambda r: r['latency_ms'].update(max=2000),
                           lambda r: r['latency_ms'].update(max=float('nan')),
                           lambda r: r.update(errors={'URLError': 1}),
                           lambda r: r['progress_samples'][2].update(active_users=999),
                           lambda r: r['progress_samples'][-1].update(elapsed_seconds=300),
                           lambda r: r['status_counts'].update({'503': 1})):
                row = completed()
                change(row)
                with self.assertRaises(ValueError):
                    s.validate_load(row, RUN, 1000, True, duration=420)

    def test_policy_is_fixed_and_legacy_is_default_in_probe(self):
        with patch.dict(probe.os.environ, {}, clear=True):
            self.assertEqual(probe.queue_policy(), 'depth-10s-v1')
        with patch.dict(probe.os.environ, {'BROWSE_QUEUE_STOP_POLICY': s.FULL_PROFILE_POLICY}):
            self.assertEqual(probe.queue_policy(), s.FULL_PROFILE_POLICY)
        with patch.dict(probe.os.environ, {'BROWSE_QUEUE_STOP_POLICY': 'ignore-everything'}):
            with self.assertRaises(read.CheckFailed):
                probe.queue_policy()

    def test_cli_uses_new_job_and_reuses_image_with_retained_wait_metrics(self):
        with ExitStack() as stack:
            run = stack.enter_context(patch.object(q, 'run'))
            for name in ('DURABLE_EVIDENCE', 'WAIT_TIME_MEASUREMENT', 'QUEUE_DEPTH_OBSERVATION_ONLY'):
                stack.enter_context(patch.object(q.s, name, False))
            stack.enter_context(patch.object(q.s, 'QUEUE32_PROFILE_JOB', 'old'))
            stack.enter_context(patch.object(q.argparse.ArgumentParser, 'parse_args',
                                              return_value=SimpleNamespace(action='run-full-profile')))
            self.assertEqual(q.main(), 0)
            run.assert_called_once_with(reuse_image=True)
            self.assertEqual(q.s.QUEUE32_PROFILE_JOB, 'browse-users-1000-full7m-v1')
            self.assertTrue(q.s.QUEUE_DEPTH_OBSERVATION_ONLY)
            self.assertTrue(q.s.DURABLE_EVIDENCE and q.s.WAIT_TIME_MEASUREMENT)

    def test_manifest_and_retained_record_pin_the_changed_criterion(self):
        app = {'spec': {'template': {'spec': {'containers': [{'image': 'fixed'}]}}}}
        manifest = {'metadata': {}, 'spec': {'template': {'spec': {'containers': [{'name': 'check'}]}}}}
        job = {'metadata': {'uid': 'owned', 'name': 'browse-users-1000-full7m-v1',
                            'labels': {'trafficxx.io/run-id': RUN}}}
        submitted = []
        def kube(*args, **kwargs):
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [{'metadata': {}, 'spec': {'nodeName': 'existing'}}]})
            if args[:2] == ('patch', '--local'):
                return json.dumps(manifest)
            if args[:2] == ('create', '-f'):
                row = json.loads(kwargs.get('data', '{}'))
                if 'spec' in row:
                    submitted.append(row)
                    return json.dumps(job)
            return '{}'
        with ExitStack() as stack:
            for name in ('QUEUE_DEPTH_OBSERVATION_ONLY', 'WAIT_TIME_MEASUREMENT', 'DURABLE_EVIDENCE'):
                stack.enter_context(patch.object(s, name, True))
            stack.enter_context(s.queue_expectation(32))
            stack.enter_context(patch.object(s.base, 'kube', side_effect=kube))
            stack.enter_context(patch.object(s.evidence, 'prepare', return_value='evidence'))
            stack.enter_context(patch.object(s.evidence, 'bind'))
            save = stack.enter_context(patch.object(s.evidence, 'save'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            s.create_job(app, RUN, job['metadata']['name'], users=1000, duration=420)
        row = submitted[0]
        self.assertEqual(row['metadata']['annotations']['trafficxx.io/queue-stop-policy'], s.FULL_PROFILE_POLICY)
        env = row['spec']['template']['spec']['containers'][0]['env']
        self.assertIn({'name': 'BROWSE_QUEUE_STOP_POLICY', 'value': s.FULL_PROFILE_POLICY}, env)
        self.assertEqual(save.call_args.args[2], 'policy.json')
        self.assertEqual(save.call_args.args[3]['request_stop_seconds'], 2)
        self.assertEqual(save.call_args.args[3]['http_error_tolerance'], 0)

    def test_report_requires_scrape_tail_error_counters_and_complete_observer(self):
        report = {'run_id': RUN, 'workload': completed(), 'counter_increases': dict.fromkeys(
            ('queue_full_total', 'acquire_timeouts_total', 'closed_rejections_total', 'borrow_errors_total',
             'connection_errors_total', 'connections_lost_total', 'bad_returns_total'), 0),
            'observer_audit': {'available': True, 'primary_failure': None,
                               'stage': 'verify_final_database_metrics', 'peak_ready_replicas': 4,
                               'queue_stop_policy': q.s.FULL_PROFILE_POLICY},
            'post_job_sample_coverage': {'all_original_instances_sampled_after_terminal': True,
                                         'instances_at_terminal': 4}}
        with patch.object(q.s, 'QUEUE_DEPTH_OBSERVATION_ONLY', True):
            q.validate_full_report(report)
            for change in (lambda r: r['counter_increases'].update(queue_full_total=1),
                           lambda r: r['counter_increases'].update(borrow_errors_total=None),
                           lambda r: r['post_job_sample_coverage'].clear(),
                           lambda r: r['post_job_sample_coverage'].update(instances_at_terminal=2),
                           lambda r: r['observer_audit'].update(stage='observe_pool_pressure'),
                           lambda r: r['observer_audit'].pop('primary_failure'),
                           lambda r: r['observer_audit'].update(primary_failure={'check': 'METRIC_STALE'}),
                           lambda r: r['observer_audit'].update(available=False)):
                row = deepcopy(report)
                change(row)
                with self.assertRaises(ValueError):
                    q.validate_full_report(row)

    def test_disappeared_job_report_cannot_skip_validation_or_restoration(self):
        with ExitStack() as stack:
            for name in ('DURABLE_EVIDENCE', 'WAIT_TIME_MEASUREMENT', 'QUEUE_DEPTH_OBSERVATION_ONLY'):
                stack.enter_context(patch.object(q.s, name, True))
            for name in ('context', 'no_active_load', 'health'):
                stack.enter_context(patch.object(q.s.trial, name))
            stack.enter_context(patch.object(q.s.trial, 'deployment', return_value={'metadata': {}}))
            stack.enter_context(patch.object(q.s.base, 'kube', return_value=''))
            stack.enter_context(patch.object(q.s.base, 'command', return_value='{"result":"BROWSE_TIMING_BASELINE_OK"}'))
            stack.enter_context(patch.object(q, 'patch_queue'))
            stack.enter_context(patch.object(q.s, 'drill_prewarmed'))
            restore = stack.enter_context(patch.object(q, 'restore'))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(ValueError, 'FULL_PROFILE_REPORT_MISSING'):
                q.run(reuse_image=True)
            restore.assert_called_once()


if __name__ == '__main__':
    unittest.main()
