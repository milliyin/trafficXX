"""Cloud Shell: 12 sequential availability reads per pod with server timing deltas."""
import argparse
import importlib.util
import json
from pathlib import Path
import time


spec = importlib.util.spec_from_file_location('timing_flow', Path(__file__).with_name('check-browse-concurrency.py'))
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)
PHASES = ('acquire', 'body', 'return', 'event_query', 'units_query')


def phases(stats):
    return (('acquire', 'body', 'return', 'availability_query')
            if 'timing_availability_query_count' in stats else PHASES)


def no_active_load():
    jobs = json.loads(flow.kubectl('get', 'jobs', '-n', 'trafficxx', '-o', 'json'))['items']
    for row in jobs:
        name = row['metadata']['name']
        if name == 'read-load-baseline' or name.startswith('browse-users-'):
            flow.require(any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                             for c in row.get('status', {}).get('conditions', [])),
                         'Another read load is active; inspect it first')


def snapshot(call, index, *, pool_size=4, queue_limit=16):
    row = flow.snapshot(call, index, expected_pool_max=pool_size,
                        **({'expected_queue_limit': queue_limit} if queue_limit != 16 else {}))
    stats = row['statistics']
    flow.require(stats['pool_min'] == pool_size and stats['pool_size'] == pool_size
                 and stats.get('pool_waiting_limit') == queue_limit, 'Expected unchanged warm pool policy')
    flow.require(all(type(stats.get('timing_' + phase + '_' + suffix)) is int
                     and stats['timing_' + phase + '_' + suffix] >= 0
                     for phase in phases(stats) for suffix in ('count', 'total_us', 'max_us')),
                 'Deploy Browse timing instrumentation first')
    return row


def experiment(call, *, pool_size=4, require_health_check=False, check_interval=None, queue_limit=16):
    flow.require(type(pool_size) is int and pool_size in (4, 8), 'Unreviewed timing pool size')
    # Verify both have instrumentation before generating the measured reads.
    flow.require(type(queue_limit) is int and queue_limit in (16, 32), 'Unreviewed timing queue size')
    initial = [snapshot(call, index, pool_size=pool_size, queue_limit=queue_limit) for index in range(2)]
    if check_interval is not None:
        flow.require(check_interval in (0, 1) and all(
            row['statistics'].get('health_check_interval_seconds') == check_interval
            and type(row['statistics'].get('health_checks_skipped')) is int
            for row in initial), 'Expected exact connection check policy on both pods')
    if require_health_check:
        flow.require(all(all(type(row['statistics'].get('timing_health_check_' + key)) is int
                             and row['statistics']['timing_health_check_' + key] >= 0
                             for key in ('count', 'total_us', 'max_us')) for row in initial),
                     'Deploy health check timing instrumentation first')
    modes = [row['statistics'].get('single_statement_autocommit', 0) for row in initial]
    flow.require(modes[0] in (0, 1) and modes[0] == modes[1], 'Browse replicas have different read modes')
    flow.require(phases(initial[0]['statistics']) == phases(initial[1]['statistics']),
                 'Browse replicas have different query timing policies')
    results = []
    for index in range(2):
        before = initial[index]
        stats = before['statistics']
        wall = []
        for number in range(12):
            route = 'general' if number % 2 == 0 else 'seated'
            begun = time.perf_counter()
            body = call(index, '/events/demo-' + route + '/availability')
            wall.append((time.perf_counter() - begun) * 1000)
            flow.require(body.get('id') == 'demo-' + route and body.get('available') == 100,
                         'Demo inventory changed')
        after = snapshot(call, index, pool_size=pool_size, queue_limit=queue_limit)
        flow.require(after['instance_id'] == before['instance_id'], 'Browse process changed')
        final = after['statistics']
        flow.require(final.get('single_statement_autocommit', 0) == modes[index], 'Read mode changed')
        if check_interval is not None:
            flow.require(final.get('health_check_interval_seconds') == check_interval
                         and final.get('health_checks_skipped', -1) >= stats['health_checks_skipped'],
                         'Connection check policy or counters changed')
        for key in ('requests_errors', 'connections_errors', 'connections_lost', 'returns_bad',
                    'rejections_queue_full', 'rejections_timeout', 'rejections_closed'):
            flow.require(type(stats.get(key)) is int and final.get(key) == stats[key], 'Pool error changed')
        timings = {}
        flow.require(phases(final) == phases(stats), 'Query timing policy changed')
        for phase in phases(stats) + (('health_check',) if require_health_check else ()):
            prefix = 'timing_' + phase + '_'
            count = final[prefix + 'count'] - stats[prefix + 'count']
            micros = final[prefix + 'total_us'] - stats[prefix + 'total_us']
            minimum = 0 if phase == 'health_check' and check_interval == 1 else 12
            flow.require(count >= minimum and micros >= 0, 'Timing evidence incomplete or reset')
            timings[phase] = {'observations': count, 'mean_ms': round(micros / count / 1000, 3) if count else None}
        if check_interval == 1:
            skipped = final['health_checks_skipped'] - stats['health_checks_skipped']
            flow.require(skipped + timings['health_check']['observations'] >= 12, 'Check policy evidence incomplete')
            timings['health_checks_skipped'] = skipped
        results.append({'reads': 12, 'timings': timings,
                        'mean_port_forward_http_ms': round(sum(wall) / len(wall), 3)})
    return {'result': 'BROWSE_TIMING_BASELINE_OK', 'per_pod': results,
            'pool_per_pod': pool_size,
            'read_mode': 'single-statement-autocommit' if modes[0] else 'transaction',
            'query_mode': 'single-query' if 'availability_query' in phases(initial[0]['statistics']) else 'two-query',
            'inventory_writes': False, 'concurrent_workload_requests': 1,
            'note': 'Timing deltas may include background readiness/other traffic. Acquisition includes checkout checks; '
                    'body includes queries and response construction; return includes commit/rollback and pool return. '
                    'Query time includes network and result fetching, not isolated server execution. No load capacity claim.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool-size', type=int, choices=(4, 8), default=4,
                        help='Require this exact reviewed warm pool size on both pods (current autoscaled policy: 8).')
    parser.add_argument('--require-health-check', action='store_true')
    parser.add_argument('--check-interval', type=int, choices=(0, 1))
    parser.add_argument('--queue-limit', type=int, choices=(16, 32), default=16)
    args = parser.parse_args()
    raise SystemExit(flow.main(experiment_function=lambda call: experiment(call, pool_size=args.pool_size,
                                                                         require_health_check=args.require_health_check,
                                                                         check_interval=args.check_interval,
                                                                         queue_limit=args.queue_limit),
                               failure_result='BROWSE_TIMING_BASELINE_FAILED',
                               preflight_check=no_active_load))
