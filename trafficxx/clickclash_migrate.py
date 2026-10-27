"""Separate, additive migration ledger; never touches ticket tables."""
import hashlib
from pathlib import Path

from trafficxx.db import connect


def run():
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(8432001)')
        conn.execute('CREATE TABLE IF NOT EXISTS public.clickclash_migrations ('
                     'version text PRIMARY KEY, checksum text NOT NULL)')
        for path in sorted((Path(__file__).resolve().parent.parent / 'database' / 'clickclash').glob('*.sql')):
            sql = path.read_text(encoding='utf-8')
            digest = hashlib.sha256(sql.encode()).hexdigest()
            previous = conn.execute('SELECT checksum FROM public.clickclash_migrations WHERE version=%s',
                                    (path.name,)).fetchone()
            if previous:
                if previous['checksum'] != digest:
                    raise RuntimeError('Applied ClickClash migration changed')
                continue
            conn.execute(sql)
            conn.execute('INSERT INTO public.clickclash_migrations VALUES (%s,%s)', (path.name, digest))
    print('CLICKCLASH_SCHEMA_OK')


if __name__ == '__main__':
    run()
