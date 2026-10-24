"""Credential-free numeric exposition of each API's process-local pool."""
from trafficxx.pool_timing import PHASES

METRICS = {
    'size': ('gauge', 'pool_size', 'Pool slots, including connections being prepared.'),
    'limit': ('gauge', 'pool_max', 'Configured maximum pool slots per process.'),
    'available': ('gauge', 'pool_available', 'Idle connections available for borrowing.'),
    'waiting_limit': ('gauge', 'pool_waiting_limit', 'Configured maximum queued borrowers per process.'),
    'waiting': ('gauge', 'requests_waiting', 'Requests waiting for a pool connection.'),
    'borrows_total': ('counter', 'requests_num', 'Connection borrows requested since process start.'),
    'borrow_errors_total': ('counter', 'requests_errors', 'Failed borrows since process start.'),
    'connection_attempts_total': ('counter', 'connections_num', 'Physical connection attempts since process start.'),
    'connection_errors_total': ('counter', 'connections_errors', 'Failed physical connection attempts.'),
    'connections_lost_total': ('counter', 'connections_lost', 'Connections found lost during checkout checks.'),
    'queue_full_total': ('counter', 'rejections_queue_full', 'Pool queue-full rejections since process start.'),
    'acquire_timeouts_total': ('counter', 'rejections_timeout', 'Pool acquisition timeouts since process start.'),
    'closed_rejections_total': ('counter', 'rejections_closed', 'Closed-pool rejections since process start.'),
    'bad_returns_total': ('counter', 'returns_bad', 'Connections returned in an unusable state.'),
}


def render(statistics, component='browse'):
    if component not in ('browse', 'booking'):
        raise ValueError('Unsupported API pool metrics component')
    lines = []
    definitions = dict(METRICS)
    if component == 'browse':
        for phase in PHASES:
            for suffix, key_suffix, kind in (('count_total', 'count', 'counter'),
                                            ('microseconds_total', 'total_us', 'counter'),
                                            ('max_microseconds', 'max_us', 'gauge')):
                definitions['timing_' + phase + '_' + suffix] = (
                    kind, 'timing_' + phase + '_' + key_suffix,
                    'Client-observed ' + phase + ' timing since process start; includes network time.')
    for suffix, (kind, key, description) in definitions.items():
        value = statistics.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError('Invalid numeric pool statistic')
        name = 'trafficxx_' + component + '_pool_' + suffix
        lines.extend((f'# HELP {name} {description}', f'# TYPE {name} {kind}', f'{name} {value}'))
    return '\n'.join(lines) + '\n'
