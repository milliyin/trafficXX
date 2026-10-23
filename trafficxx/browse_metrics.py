"""Credential-free Prometheus exposition of process-local Browse pool counters."""
METRICS = {
    'size': ('gauge', 'pool_size', 'Pool slots, including connections being prepared.'),
    'limit': ('gauge', 'pool_max', 'Configured maximum pool slots per process.'),
    'available': ('gauge', 'pool_available', 'Idle connections available for borrowing.'),
    'waiting': ('gauge', 'requests_waiting', 'Requests waiting for a pool connection.'),
    'borrows_total': ('counter', 'requests_num', 'Connection borrows requested since process start.'),
    'borrow_errors_total': ('counter', 'requests_errors', 'Failed borrows since process start.'),
    'connection_attempts_total': ('counter', 'connections_num', 'Physical connection attempts since process start.'),
    'connection_errors_total': ('counter', 'connections_errors', 'Failed physical connection attempts.'),
    'connections_lost_total': ('counter', 'connections_lost', 'Connections found lost during checkout checks.'),
    'bad_returns_total': ('counter', 'returns_bad', 'Connections returned in an unusable state.'),
}


def render(statistics):
    lines = []
    for suffix, (kind, key, description) in METRICS.items():
        value = statistics.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError('Invalid numeric pool statistic')
        name = 'trafficxx_browse_pool_' + suffix
        lines.extend((f'# HELP {name} {description}', f'# TYPE {name} {kind}', f'{name} {value}'))
    return '\n'.join(lines) + '\n'
