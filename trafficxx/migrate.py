"""Apply additive, checksummed migrations without resetting existing lab data."""
import hashlib
import logging
from pathlib import Path

from trafficxx.db import connect


def run():
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(7432002)')
        conn.execute('SELECT 1 FROM outbox LIMIT 1')  # The existing bootstrap must have run.
        conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                     'version text PRIMARY KEY, checksum text NOT NULL, '
                     'applied_at timestamptz NOT NULL DEFAULT clock_timestamp())')
        for path in sorted((Path(__file__).resolve().parent.parent / 'database' / 'migrations').glob('*.sql')):
            sql = path.read_text(encoding='utf-8')
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            previous = conn.execute('SELECT checksum FROM schema_migrations WHERE version = %s',
                                    (path.name,)).fetchone()
            if previous:
                if previous['checksum'] != checksum:
                    raise RuntimeError(f'Applied migration was edited: {path.name}')
                continue
            conn.execute(sql)
            conn.execute('INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)',
                         (path.name, checksum))
            logging.info('Applied migration %s', path.name)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run()
