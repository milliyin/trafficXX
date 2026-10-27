"""Operator actions over private DB access, never through a public admin route."""
import argparse
import json
from uuid import UUID

from trafficxx.db import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pause', 'resume', 'hide-ad', 'show-ad', 'remove-ad', 'ban', 'unban', 'reports', 'cleanup'])
    parser.add_argument('--player', type=UUID)
    args = parser.parse_args()
    if args.action in ('hide-ad', 'show-ad', 'remove-ad', 'ban', 'unban') and args.player is None:
        parser.error('--player UUID is required')
    with connect() as conn:
        if args.action in ('pause', 'resume'):
            conn.execute('UPDATE clickclash.control SET paused=%s WHERE id', (args.action == 'pause',))
        elif args.action in ('hide-ad', 'show-ad'):
            conn.execute('UPDATE clickclash.players SET ad_hidden=%s WHERE id=%s', (args.action == 'hide-ad', args.player))
        elif args.action in ('ban', 'unban'):
            conn.execute('UPDATE clickclash.players SET banned=%s WHERE id=%s', (args.action == 'ban', args.player))
            if args.action == 'ban':
                conn.execute('DELETE FROM clickclash.sessions WHERE player_id=%s', (args.player,))
        elif args.action == 'remove-ad':
            conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
            stored = conn.execute('DELETE FROM clickclash.image_blobs WHERE player_id=%s RETURNING byte_size', (args.player,)).fetchone()
            if stored:
                conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (stored['byte_size'],))
            removed = conn.execute('DELETE FROM clickclash.images WHERE player_id=%s RETURNING octet_length(content) AS size', (args.player,)).fetchone()
            if removed:
                conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (removed['size'],))
            conn.execute("UPDATE clickclash.players SET bio='',website='',ad_hidden=true WHERE id=%s", (args.player,))
        elif args.action == 'reports':
            rows = conn.execute('SELECT target,reason,created_at FROM clickclash.reports ORDER BY created_at DESC LIMIT 100').fetchall()
            print(json.dumps({'reports': rows}, default=str))
            return
        elif args.action == 'cleanup':
            conn.execute('DELETE FROM clickclash.sessions WHERE expires_at < clock_timestamp()')
            conn.execute("DELETE FROM clickclash.buckets WHERE bucket_start < extract(epoch FROM clock_timestamp()-interval '2 days')")
            # UUID receipts are retained: deleting them would allow an old retry to score again.
        conn.execute('INSERT INTO clickclash.operator_audit(action,target) VALUES (%s,%s)', (args.action, args.player))
    print(json.dumps({'result': 'CLICKCLASH_OPERATOR_OK', 'action': args.action}))


if __name__ == '__main__':
    main()
