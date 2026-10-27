"""Resumable banner migration and bounded orphan cleanup, inside the private app pod."""
import argparse
from datetime import datetime, timedelta, timezone
import json

from trafficxx import clickclash_blobs as blobs
from trafficxx.db import connect


def migrate(limit=100):
    moved = 0
    for _ in range(limit):
        with connect() as conn:
            conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
            row = conn.execute('SELECT * FROM clickclash.images ORDER BY player_id LIMIT 1 FOR UPDATE').fetchone()
            if not row:
                break
            raw = bytes(row['content'])
            blob_name, digest = blobs.put(row['player_id'], raw)
            previous = conn.execute('SELECT byte_size FROM clickclash.image_blobs WHERE player_id=%s', (row['player_id'],)).fetchone()
            conn.execute('INSERT INTO clickclash.image_blobs VALUES (%s,%s,%s,%s) ON CONFLICT(player_id) '
                         'DO UPDATE SET blob_name=excluded.blob_name,digest=excluded.digest,byte_size=excluded.byte_size',
                         (row['player_id'], blob_name, digest, len(raw)))
            conn.execute('DELETE FROM clickclash.images WHERE player_id=%s', (row['player_id'],))
            if previous:
                conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (previous['byte_size'],))
        moved += 1
    with connect() as conn:
        remaining = conn.execute('SELECT count(*) AS n FROM clickclash.images').fetchone()['n']
    return {'result': 'CLICKCLASH_BLOB_MIGRATION_OK', 'moved': moved, 'remaining': remaining}


def verify():
    blobs.container().get_container_properties(timeout=5)
    with connect() as conn:
        # Serialize against replacements/deletions while checking current objects.
        conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
        rows = conn.execute('SELECT * FROM clickclash.image_blobs ORDER BY player_id LIMIT 201').fetchall()
        if len(rows) > 200:
            raise RuntimeError('Verification batch exceeds limit')
        for row in rows:
            data = blobs.read(row['blob_name'], row['player_id'], row['digest'])
            if len(data) != row['byte_size']:
                raise blobs.StorageUnavailable()
        remaining = conn.execute('SELECT count(*) AS n FROM clickclash.images').fetchone()['n']
    return {'result': 'CLICKCLASH_BLOB_CHECK_OK', 'verified': len(rows), 'legacy_remaining': remaining}


def cleanup(execute=False, cursor=None):
    candidates, removed = 0, 0
    # Keep old immutable objects beyond the configured 14-day PostgreSQL restore window.
    cutoff = datetime.now(timezone.utc) - timedelta(days=15)
    pages = blobs.container().list_blobs(name_starts_with='banners/', results_per_page=100).by_page(continuation_token=cursor)
    page = next(pages, ())
    for blob in page:
        if not blob.last_modified or blob.last_modified >= cutoff:
            continue
        parts = blob.name.split('/')
        if len(parts) != 3 or not parts[2].endswith('.webp'):
            continue
        try:
            expected = blobs.name(parts[1], parts[2][:-5])
        except ValueError:
            continue
        if expected != blob.name:
            continue
        with connect() as conn:
            conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
            if conn.execute('SELECT 1 FROM clickclash.image_blobs WHERE blob_name=%s', (blob.name,)).fetchone():
                continue
            candidates += 1
            if execute:
                blobs.delete_unreferenced(conn, parts[1], parts[2][:-5])
                removed += 1
    return {'result': 'CLICKCLASH_BLOB_CLEANUP_OK', 'candidates': candidates, 'removed': removed,
            'dry_run': not execute, 'next_cursor': pages.continuation_token}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['migrate', 'check', 'cleanup'])
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--cursor', help='Continue a prior cleanup page')
    args = parser.parse_args()
    if not blobs.enabled():
        raise RuntimeError('Blob storage must be configured')
    value = migrate() if args.action == 'migrate' else verify() if args.action == 'check' else cleanup(args.execute, args.cursor)
    print(json.dumps(value))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'result': 'CLICKCLASH_BLOB_MAINTENANCE_FAILED', 'error_type': type(error).__name__,
                          'note': 'Existing database image is retained on a failed migration; rerun to resume.'}))
        raise SystemExit(1) from None
