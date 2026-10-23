"""One bounded pool per Browse process, owned by the application lifespan."""
from contextlib import contextmanager
import os

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


class BrowseDatabase:
    def __init__(self):
        self.pool = None

    def open(self):
        if self.pool is not None:
            raise RuntimeError('Browse database pool already open')
        try:
            maximum = int(os.environ.get('BROWSE_DB_POOL_MAX', '4'))
            if not 1 <= maximum <= 8:
                raise ValueError
        except ValueError:
            raise ValueError('BROWSE_DB_POOL_MAX must be an integer from 1 to 8') from None
        # Callable kwargs run on EVERY physical connection attempt, not once at
        # startup. No token/password is frozen in the pool configuration.
        pool = ConnectionPool(
            conninfo=pool_conninfo, kwargs=pool_parameters,
            min_size=1, max_size=maximum,
            timeout=3, max_waiting=8, max_lifetime=1800, max_idle=60,
            check=ConnectionPool.check_connection, open=False,
            name='browse', num_workers=2,
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
            raise OperationalError('Browse database pool is not running')
        try:
            # Pool context commits/rolls back and RETURNS the connection; using
            # the raw connection context here instead would close the socket.
            with pool.connection() as conn:
                yield conn
        except (PoolTimeout, TooManyRequests, PoolClosed):
            raise HTTPException(503, detail={'code': 'DATABASE_BUSY'}) from None
