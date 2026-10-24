"""Browse pool policy; preserve the existing import used by diagnostics."""
from contextlib import contextmanager
from time import perf_counter

from trafficxx.database_pool import DatabasePool
from trafficxx.pool_timing import PoolTimings


class BrowseDatabase(DatabasePool):
    def __init__(self):
        super().__init__('browse', 'BROWSE_DB_POOL_MAX', 'BROWSE_DB_POOL_MIN', 'BROWSE_DB_POOL_MAX_WAITING')
        self.timings = PoolTimings()

    def statistics(self):
        row = super().statistics()
        row['statistics'].update(self.timings.statistics())
        return row

    @contextmanager
    def connection(self):
        begun, returning = perf_counter(), None
        try:
            with super().connection() as conn:
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
