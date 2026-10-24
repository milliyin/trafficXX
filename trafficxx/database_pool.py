"""Bounded process-local API pools with transaction-scoped borrowing."""
from contextlib import contextmanager
import os
from threading import Lock
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
    def __init__(self, name, size_variable, minimum_variable=None, waiting_variable=None):
        self.name = name
        self.size_variable = size_variable
        self.minimum_variable = minimum_variable
        self.waiting_variable = waiting_variable
        self._waiting_limit = 8
        self.pool = None
        self.instance_id = uuid4().hex
        self._rejection_lock = Lock()
        self._rejections = dict(rejections_queue_full=0, rejections_timeout=0, rejections_closed=0)

    def statistics(self):
        if self.pool is None:
            raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE'})
        values = self.pool.get_stats()  # Non-destructive; do not reset counters.
        keys = ('pool_min', 'pool_max', 'pool_size', 'pool_available',
                'requests_waiting', 'requests_num', 'requests_errors',
                'connections_num', 'connections_errors', 'connections_lost', 'returns_bad')
        with self._rejection_lock:
            rejections = dict(self._rejections)
        return {'instance_id': self.instance_id,
                'statistics': {**{key: values.get(key, 0) for key in keys}, **rejections,
                               'pool_waiting_limit': self._waiting_limit}}

    def open(self):
        if self.pool is not None:
            raise RuntimeError('Database pool already open')
        try:
            maximum = int(os.environ.get(self.size_variable, '4'))
            minimum = int(os.environ.get(self.minimum_variable, '1')) if self.minimum_variable else 1
            if not 1 <= minimum <= maximum <= 8:
                raise ValueError
        except ValueError:
            raise ValueError('Pool bounds must be integers with 1 <= minimum <= maximum <= 8') from None
        try:
            waiting = int(os.environ.get(self.waiting_variable, '8')) if self.waiting_variable else 8
            if waiting not in (8, 16):
                raise ValueError
        except ValueError:
            raise ValueError('Pool waiting limit must be 8 or 16') from None
        # Callable kwargs run on EVERY physical connection attempt, not once at
        # startup. No token/password is frozen in the pool configuration.
        pool = ConnectionPool(
            conninfo=pool_conninfo, kwargs=pool_parameters,
            min_size=minimum, max_size=maximum,
            timeout=3, max_waiting=waiting, max_lifetime=1800, max_idle=60,
            check=ConnectionPool.check_connection, open=False,
            name=self.name, num_workers=2,
        )
        try:
            pool.open(wait=False)
            if minimum > 1:
                pool.wait(timeout=30)
        except BaseException:
            pool.close()
            raise
        self._waiting_limit = waiting
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
        except (PoolTimeout, TooManyRequests, PoolClosed) as error:
            key = ('rejections_queue_full' if isinstance(error, TooManyRequests) else
                   'rejections_timeout' if isinstance(error, PoolTimeout) else 'rejections_closed')
            with self._rejection_lock:
                self._rejections[key] += 1
            raise HTTPException(503, detail={'code': 'DATABASE_BUSY'}) from None
