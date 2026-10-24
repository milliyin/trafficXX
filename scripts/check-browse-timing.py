"""Cloud Shell: 12 sequential availability reads per pod with server timing deltas."""
import importlib.util
import json
from pathlib import Path
import time


spec = importlib.util.spec_from_file_location('timing_flow', Path(__file__).with_name('check-browse-concurrency.py'))
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)
PHASES = ('acquire', 'body', 'return', 'event_query', 'units_query')


def no_active_load():
    jobs = json.loads(flow.kubectl('get', 'jobs', '-n', 'trafficxx', '-o', 'json'))['items']
    for row in jobs:
        name = row['metadata']['name']
        if name == 'read-load-baseline' or name.startswith('browse-users-'):
            flow.require(any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                             for c in row.get('status', {}).get('conditions', [])),
                         'Another read load is active; inspect it first')


def snapshot(call, index):
    row = flow.snapshot(call, index)
    stats = row['statistics']
    flow.require(stats['pool_min'] == 4 and stats['pool_size'] == 4
                 and stats.get('pool_waiting_limit') == 16, 'Expected unchanged warm pool policy')
    flow.require(all(type(stats.get('timing_' + phase + '_' + suffix)) is int
                     and stats['timing_' + phase + '_' + suffix] >= 0
                     for phase in PHASES for suffix in ('count', 'total_us', 'max_us')),
                 'Deploy Browse timing instrumentation first')
    return row


def experiment(call):
    # Verify both have instrumentation before generating the measured reads.
    initial = [snapshot(call, index) for index in range(2)]
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
        after = snapshot(call, index)
        flow.require(after['instance_id'] == before['instance_id'], 'Browse process changed')
        final = after['statistics']
        for key in ('requests_errors', 'connections_errors', 'connections_lost', 'returns_bad',
                    'rejections_queue_full', 'rejections_timeout', 'rejections_closed'):
            flow.require(type(stats.get(key)) is int and final.get(key) == stats[key], 'Pool error changed')
        timings = {}
        for phase in PHASES:
            prefix = 'timing_' + phase + '_'
            count = final[prefix + 'count'] - stats[prefix + 'count']
            micros = final[prefix + 'total_us'] - stats[prefix + 'total_us']
            flow.require(count >= 12 and micros >= 0, 'Timing evidence incomplete or reset')
            timings[phase] = {'observations': count, 'mean_ms': round(micros / count / 1000, 3)}
        results.append({'reads': 12, 'timings': timings,
                        'mean_port_forward_http_ms': round(sum(wall) / len(wall), 3)})
    return {'result': 'BROWSE_TIMING_BASELINE_OK', 'per_pod': results,
            'inventory_writes': False, 'concurrent_workload_requests': 1,
            'note': 'Timing deltas may include background readiness/other traffic. Acquisition includes checkout checks; '
                    'body includes queries and response construction; return includes commit/rollback and pool return. '
                    'Query time includes network and result fetching, not isolated server execution. No load capacity claim.'}


if __name__ == '__main__':
    raise SystemExit(flow.main(experiment_function=experiment, failure_result='BROWSE_TIMING_BASELINE_FAILED',
                               preflight_check=no_active_load))
