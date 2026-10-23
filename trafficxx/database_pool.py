"""Bounded process-local API pools with transaction-scoped borrowing."""
from contextlib import contextmanager
import os
from uuid import uuid4

from fastapi import HTTPException
from psycopg import OperationalError
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from trafficxx.db import connection_parameters


def pool_conninfo():
    if os.environ.get('DATABASE_AUTH_MODE', 'url') == 'url':
        return os.environ['DATABASE_URL']
    return ''


def pool_parameters():
    parameters = connection_parameters()
    parameters.pop('conninfo', None)  # Pool passes conninfo as a positional argument.
    return parameters


class DatabasePool:
    def __init__(self, name, size_variable):
        self.name = name
        self.size_variable = size_variable
        self.pool = None
        self.instance_id = uuid4().hex

    def statistics(self):
        if self.pool is None:
            raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE'})
        values = self.pool.get_stats()  # Non-destructive; do not reset counters.
        keys = ('pool_min', 'pool_max', 'pool_size', 'pool_available',
                'requests_waiting', 'requests_num', 'requests_errors',
                'connections_num', 'connections_errors', 'connections_lost', 'returns_bad')
        return {'instance_id': self.instance_id,
                'statistics': {key: values.get(key, 0) for key in keys}}

    def open(self):
        if self.pool is not None:
            raise RuntimeError('Database pool already open')
        try:
            maximum = int(os.environ.get(self.size_variable, '4'))
            if not 1 <= maximum <= 8:
                raise ValueError
        except ValueError:
            raise ValueError(self.size_variable + ' must be an integer from 1 to 8') from None
        # Callable kwargs run on EVERY physical connection attempt, not once at
        # startup. No token/password is frozen in the pool configuration.
        pool = ConnectionPool(
            conninfo=pool_conninfo, kwargs=pool_parameters,
            min_size=1, max_size=maximum,
            timeout=3, max_waiting=8, max_lifetime=1800, max_idle=60,
            check=ConnectionPool.check_connection, open=False,
            name=self.name, num_workers=2,
        )
        try:
            pool.open(wait=False)
        except BaseException:
            pool.close()
            raise
        self.pool = pool

    def close(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.close()

    @contextmanager
    def connection(self):
        pool = self.pool
        if pool is None:
            raise OperationalError('Database pool is not running')
        try:
            # Pool context commits/rolls back and RETURNS the connection; using
            # the raw connection context here instead would close the socket.
            with pool.connection() as conn:
                yield conn
        except (PoolTimeout, TooManyRequests, PoolClosed):
            raise HTTPException(503, detail={'code': 'DATABASE_BUSY'}) from None
