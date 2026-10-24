"""Fixed-name, numeric process timings; no SQL text or request labels."""
from contextlib import contextmanager
from threading import Lock
from time import perf_counter


PHASES = ('acquire', 'body', 'return', 'event_query', 'units_query', 'availability_query')


class PoolTimings:
    def __init__(self):
        self._lock = Lock()
        self._values = {phase: [0, 0, 0] for phase in PHASES}

    def record(self, phase, seconds):
        micros = max(0, round(seconds * 1_000_000))
        with self._lock:
            row = self._values[phase]
            row[0] += 1
            row[1] += micros
            row[2] = max(row[2], micros)

    @contextmanager
    def measure(self, phase):
        if phase not in PHASES:
            raise ValueError('Unsupported timing phase')
        begun = perf_counter()
        try:
            yield
        finally:
            self.record(phase, perf_counter() - begun)

    def statistics(self):
        with self._lock:
            return {'timing_' + phase + '_' + suffix: row[index]
                    for phase, row in self._values.items()
                    for index, suffix in enumerate(('count', 'total_us', 'max_us'))}
