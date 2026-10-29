"""Browse pool policy; preserve the existing import used by diagnostics."""
from contextlib import contextmanager
from time import perf_counter

from trafficxx.database_pool import DatabasePool
from trafficxx.pool_timing import PoolTimings


class BrowseDatabase(DatabasePool):
    def __init__(self):
        super().__init__('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX_WAITING',
                         autocommit=True)
        self.timings = PoolTimings()

    def statistics(self):
        row = super().statistics()
        row['statistics'].update(self.timings.statistics())
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
