"""Timing boundaries, rollback and measurement guards without a live database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
from types import ModuleType
import unittest
from unittest.mock import patch

from trafficxx.pool_timing import PHASES, PoolTimings

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = spec_from_file_location(name, ROOT / path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Timings(unittest.TestCase):
    def test_eight_connection_timing_requires_exact_stable_warm_policy(self):
        module = load('timing_eight', 'scripts/check-browse-timing.py')
        counts = [0, 0]
        snapshots = [0, 0]
        fault = [None]
        def call(index, path):
            if path != '/internal/database-pool':
                counts[index] += 1
                return {'id': 'demo-' + ('general' if 'general' in path else 'seated'), 'available': 100}
            snapshots[index] += 1
            size = 4 if (fault[0] == 'mixed' and index == 1) or (fault[0] == 'changed' and snapshots[index] > 1) else 8
            stats = dict(pool_min=size, pool_max=size, pool_size=size, pool_available=size,
                requests_waiting=0, pool_waiting_limit=16, requests_num=counts[index], requests_errors=0,
                connections_num=size, connections_errors=0, connections_lost=0, returns_bad=0,
                rejections_queue_full=0, rejections_timeout=0, rejections_closed=0, single_statement_autocommit=1)
            for phase in PHASES:
                stats.update({'timing_' + phase + '_count': counts[index],
                    'timing_' + phase + '_total_us': counts[index] * 1000,
                    'timing_' + phase + '_max_us': 1000})
            return {'instance_id': 'ab'[index] * 32, 'statistics': stats}
        row = module.experiment(call, pool_size=8)
        self.assertEqual(row['pool_per_pod'], 8)
        self.assertEqual(row['read_mode'], 'single-statement-autocommit')
        self.assertEqual(counts, [12, 12])
        for failure in ('mixed', 'changed'):
            fault[0], counts[:], snapshots[:] = failure, [0, 0], [0, 0]
            with self.subTest(failure=failure), self.assertRaises(module.flow.CheckFailed):
                module.experiment(call, pool_size=8)
            if failure == 'mixed':
                self.assertEqual(counts, [0, 0])
        fault[0], counts[:], snapshots[:] = None, [0, 0], [0, 0]
        with self.assertRaises(module.flow.CheckFailed):
            module.experiment(call)  # Legacy default must still reject eight.
        with self.assertRaises(module.flow.CheckFailed):
            module.experiment(call, pool_size=16)
        self.assertEqual(counts, [0, 0])

    def test_probe_blocks_active_load_and_preserves_failed_job(self):
        module = load('timing_job_guard', 'scripts/check-browse-timing.py')
        jobs = {'items': [{'metadata': {'name': 'browse-users-250'}, 'status': {}}]}
        with patch.object(module.flow, 'kubectl', return_value=json.dumps(jobs)):
            with self.assertRaises(module.flow.CheckFailed):
                module.no_active_load()
        jobs['items'][0]['status']['conditions'] = [{'type': 'Failed', 'status': 'True'}]
        with patch.object(module.flow, 'kubectl', return_value=json.dumps(jobs)) as kube:
            module.no_active_load()
        self.assertEqual(kube.call_count, 1)

    def test_measure_records_failure_and_concurrent_totals(self):
        timings = PoolTimings()
        with patch('trafficxx.pool_timing.perf_counter', side_effect=[1, 1.025]):
            with self.assertRaises(ValueError):
                with timings.measure('event_query'):
                    raise ValueError('PRIVATE')
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: timings.record('acquire', .01), range(100)))
        values = timings.statistics()
        self.assertEqual(values['timing_event_query_total_us'], 25000)
        self.assertEqual(values['timing_acquire_count'], 100)
        self.assertEqual(values['timing_acquire_total_us'], 1000000)
        self.assertNotIn('PRIVATE', str(values))
        with self.assertRaises(ValueError):
            with timings.measure('customer-id'):
                self.fail('must reject arbitrary labels')

    def test_browse_boundaries_include_return_and_preserve_exceptions(self):
        events = []
        class Base:
            def __init__(self, *args, **kwargs):
                pass
            def statistics(self):
                return {'instance_id': 'a' * 32, 'statistics': {}}
            @contextmanager
            def connection(self):
                try:
                    yield 'connection'
                except ValueError:
                    events.append('rollback')
                    raise
                finally:
                    events.append('return')
        stub = ModuleType('trafficxx.database_pool')
        stub.DatabasePool = Base
        with patch.dict('sys.modules', {'trafficxx.database_pool': stub}):
            module = load('timed_browse', 'trafficxx/browse_db.py')
        database = module.BrowseDatabase()
        with patch.object(module, 'perf_counter', side_effect=[0, .01, .06, .09]):
            with self.assertRaises(ValueError):
                with database.connection() as conn:
                    self.assertEqual(conn, 'connection')
                    raise ValueError('PRIVATE')
        values = database.statistics()['statistics']
        self.assertEqual(events, ['rollback', 'return'])
        for phase, micros in (('acquire', 10000), ('body', 50000), ('return', 30000)):
            self.assertEqual(values['timing_' + phase + '_count'], 1)
            self.assertEqual(values['timing_' + phase + '_total_us'], micros)

    def test_probe_rejects_missing_timers_before_reads_and_reports_deltas(self):
        module = load('timing_probe', 'scripts/check-browse-timing.py')
        counts = [0, 0]
        def call(index, path):
            if path != '/internal/database-pool':
                counts[index] += 1
                return {'id': 'demo-' + ('general' if 'general' in path else 'seated'), 'available': 100}
            stats = dict(pool_min=4, pool_max=4, pool_size=4, pool_available=4,
                         requests_waiting=0, pool_waiting_limit=16, requests_num=counts[index],
                         requests_errors=0, connections_num=4, connections_errors=0,
                         connections_lost=0, returns_bad=0, rejections_queue_full=0,
                         rejections_timeout=0, rejections_closed=0)
            for phase in PHASES:
                stats.update({'timing_' + phase + '_count': counts[index],
                              'timing_' + phase + '_total_us': counts[index] * 1000,
                              'timing_' + phase + '_max_us': 1000})
            return {'instance_id': 'a' * 32, 'statistics': stats}
        report = module.experiment(call)
        self.assertEqual(counts, [12, 12])
        self.assertEqual(report['per_pod'][0]['timings']['availability_query']['mean_ms'], 1)
        self.assertEqual(report['query_mode'], 'single-query')
        self.assertEqual(report['read_mode'], 'transaction')
        def autocommit(index, path):
            row = call(index, path)
            if path == '/internal/database-pool':
                row['statistics']['single_statement_autocommit'] = 1
            return row
        self.assertEqual(module.experiment(autocommit)['read_mode'], 'single-statement-autocommit')
        before_modes = list(counts)
        with self.assertRaisesRegex(module.flow.CheckFailed, 'different read modes'):
            module.experiment(lambda i, p: autocommit(i, p) if i == 0 else call(i, p))
        self.assertEqual(counts, before_modes)

        def legacy(index, path):
            row = call(index, path)
            if path == '/internal/database-pool':
                for suffix in ('count', 'total_us', 'max_us'):
                    row['statistics'].pop('timing_availability_query_' + suffix)
            return row
        report = module.experiment(legacy)
        self.assertEqual(report['query_mode'], 'two-query')
        self.assertEqual(report['per_pod'][0]['timings']['event_query']['mean_ms'], 1)
        before_mixed = list(counts)
        def mixed(index, path):
            return legacy(index, path) if index == 0 else call(index, path)
        with self.assertRaisesRegex(module.flow.CheckFailed, 'different query timing policies'):
            module.experiment(mixed)
        self.assertEqual(counts, before_mixed)
        def missing(index, path):
            row = call(index, path)
            row['statistics'].pop('timing_acquire_count')
            return row
        with self.assertRaises(module.flow.CheckFailed):
            module.experiment(missing)
        self.assertEqual(counts, before_mixed)
