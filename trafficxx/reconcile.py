import argparse
import json

from trafficxx.messaging import replay_stalled


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Replay published events missing consumer receipts; dry run by default')
    parser.add_argument('--older-than-seconds', type=int, default=60)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    count = replay_stalled(args.older_than_seconds, args.apply)
    print(json.dumps({'candidates': count, 'applied': args.apply}))
