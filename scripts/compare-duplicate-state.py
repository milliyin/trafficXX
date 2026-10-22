"""Compare saved public diagnostic JSON; does not access Azure or message bodies."""
import json
from pathlib import Path
import sys


def compare(before, after):
    expected_tables = {'consumer_inbox', 'reservation_updates', 'outbox'}
    for state in (before, after):
        if (state.get('result') != 'DUPLICATE_BASELINE_OK' or state.get('outbox_id') != 3
                or state.get('message_id') != '151af373-f74d-4bc4-8c14-e3b6cf13aa51'
                or state.get('observer_identity') != 'trafficxx-lab-migrator'
                or state.get('read_only') is not True or state.get('ssl') is not True
                or set(state.get('tables', {})) != expected_tables):
            raise ValueError('Unexpected diagnostic identity, target or table set')
        for entry in state['tables'].values():
            digest = entry.get('sha256', '')
            if (not isinstance(entry.get('count'), int) or entry['count'] <= 0
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(c not in '0123456789abcdef' for c in digest)):
                raise ValueError('Invalid diagnostic count or fingerprint')
    if before['tables'] != after['tables']:
        raise ValueError('Stored state changed during the experiment')
    return {'result': 'DUPLICATE_STATE_UNCHANGED', 'message_id': before['message_id'],
            'tables': sorted(expected_tables), 'counts_and_all_columns_unchanged': True,
            'note': 'Require a new consumer duplicate log and native queue counts separately.'}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3:
            raise ValueError('Provide before and after JSON paths')
        print(json.dumps(compare(*(json.loads(Path(p).read_text(encoding='utf-8')) for p in sys.argv[1:]))))
    except Exception as error:
        print(json.dumps({'result': 'DUPLICATE_STATE_CHECK_FAILED', 'error_type': type(error).__name__}))
        sys.exit(1)
