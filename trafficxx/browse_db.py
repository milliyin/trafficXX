"""Browse pool policy; preserve the existing import used by diagnostics."""
from contextlib import contextmanager
import os
from time import perf_counter

from trafficxx.database_pool import DatabasePool
from trafficxx.pool_timing import PoolTimings


class BrowseDatabase(DatabasePool):
    def __init__(self):
        self.timings = PoolTimings()
        raw_interval = os.environ.get('BROWSE_DB_CHECK_INTERVAL_SECONDS', '0')
        if raw_interval not in ('0', '1'):
            raise ValueError('Browse check interval must be zero or one second')
        super().__init__('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX_WAITING',
                         autocommit=True, check_observer=self.timings.measure,
                         check_interval_seconds=int(raw_interval))

    def statistics(self):
        row = super().statistics()
        row['statistics'].update(self.timings.statistics())
        row['statistics'].update(self.check_statistics())
        row['statistics']['single_statement_autocommit'] = 1
        return row

    @contextmanager
    def connection(self):
        with self._timed_connection(single_statement=False) as conn:
            yield conn

    @contextmanager
    def read_connection(self):
        with self._timed_connection(single_statement=True) as conn:
            yield conn

    @contextmanager
    def _timed_connection(self, *, single_statement):
        begun, returning = perf_counter(), None
        try:
            lease = super().read_connection() if single_statement else super().connection()
            with lease as conn:
                acquired = perf_counter()
                self.timings.record('acquire', acquired - begun)
                try:
                    yield conn
                finally:
                    returning = perf_counter()
                    self.timings.record('body', returning - acquired)
        finally:
            if returning is not None:
                self.timings.record('return', perf_counter() - returning)
