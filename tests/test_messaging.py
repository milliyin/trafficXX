from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError

from trafficxx.consumer import handle_message
from trafficxx.db import connect
from trafficxx.messaging import (
    InvalidEvent, PublishError, envelope, parse_event, process_event, publish_one,
    record_dead_letter, replay_stalled,
)
from trafficxx.migrate import run
from trafficxx.models import ReservationRequest
from trafficxx.operations import app, snapshot
from trafficxx.service import reservation, reserve


def create():
    return reserve('alice', str(uuid4()), ReservationRequest(event_id='general', quantity=1))[0]


def rows():
    with connect() as conn:
        return conn.execute('SELECT * FROM outbox ORDER BY id').fetchall()


def message(event, **overrides):
    fields = dict(body=[event.model_dump_json().encode()], message_id=str(event.message_id),
                  sequence_number=event.outbox_id, delivery_count=0, dead_letter_reason=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


class Receiver:
    def __init__(self):
        self.completed = self.abandoned = self.dead_lettered = 0

    def complete_message(self, message):
        self.completed += 1

    def abandon_message(self, message):
        self.abandoned += 1

    def dead_letter_message(self, message, **kwargs):
        self.dead_lettered += 1


def test_publisher_retains_failed_send_and_retries_same_identity():
    create()
    original = rows()[0]
    def offline(event):
        raise PublishError('offline')
    assert publish_one(offline) == 'retry'
    failed = rows()[0]
    assert failed['published_at'] is None
    assert failed['publish_failures'] == failed['publish_attempts'] == 1
    assert failed['next_attempt_at'] > failed['last_attempt_at']
    assert publish_one(lambda event: pytest.fail('Backoff was ignored')) == 'idle'
    with connect() as conn:
        conn.execute('UPDATE outbox SET next_attempt_at = clock_timestamp()')
    sent = []
    assert publish_one(sent.append) == 'published'
    assert sent[0].message_id == original['message_id']
    assert rows()[0]['publish_attempts'] == 2


def test_crash_after_send_before_mark_resends_same_event(monkeypatch):
    create()
    sent = []
    def crash(conn, outbox_id):
        raise RuntimeError('Crash after broker accepted the message')
    with monkeypatch.context() as patch:
        patch.setattr('trafficxx.messaging.mark_published', crash)
        with pytest.raises(RuntimeError):
            publish_one(sent.append)
    assert rows()[0]['published_at'] is None
    assert publish_one(sent.append) == 'published'
    assert sent[0] == sent[1]
    assert process_event(sent[0]) is True
    assert process_event(sent[1]) is False


def test_parallel_publishers_claim_each_row_once():
    for _ in range(12):
        create()
    sent = []
    def drain(_):
        while publish_one(sent.append) != 'idle':
            pass
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(drain, range(4)))
    assert len(sent) == len({event.message_id for event in sent}) == 12
    assert all(row['published_at'] is not None for row in rows())


def test_concurrent_duplicate_consumers_apply_one_effect():
    create()
    event = envelope(rows()[0])
    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _: process_event(event), range(24)))
    assert sum(outcomes) == 1
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM consumer_inbox').fetchone()['n'] == 1
        assert conn.execute('SELECT count(*) AS n FROM reservation_updates').fetchone()['n'] == 1


def test_failed_effect_rolls_back_inbox_so_retry_can_process(monkeypatch):
    create()
    event = envelope(rows()[0])
    def crash(conn, event):
        raise RuntimeError('Crash during downstream effect')
    with monkeypatch.context() as patch:
        patch.setattr('trafficxx.messaging.apply_projection', crash)
        with pytest.raises(RuntimeError):
            process_event(event)
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM consumer_inbox').fetchone()['n'] == 0
    assert process_event(event) is True


def test_out_of_order_events_do_not_regress_downstream_state():
    hold = create()
    reservation('alice', hold['id'], 'CANCELLED')
    held, cancelled = [envelope(row) for row in rows()]
    assert process_event(cancelled)
    assert process_event(held)
    with connect() as conn:
        assert conn.execute('SELECT status FROM reservation_updates').fetchone()['status'] == 'CANCELLED'


def test_duplicate_with_altered_body_is_rejected():
    create()
    event = envelope(rows()[0])
    assert process_event(event)
    with pytest.raises(InvalidEvent, match='MESSAGE_ID_REUSED'):
        process_event(event.model_copy(update={'status': 'CONFIRMED'}))


def test_commit_before_ack_survives_ack_failure(monkeypatch):
    create()
    incoming = message(envelope(rows()[0]))
    receiver = Receiver()
    def lost_ack(message):
        raise RuntimeError('Acknowledgement lost')
    with monkeypatch.context() as patch:
        patch.setattr(receiver, 'complete_message', lost_ack)
        with pytest.raises(RuntimeError):
            handle_message(receiver, incoming, 'unit-test')
    assert handle_message(receiver, incoming, 'unit-test') == 'duplicate'
    assert receiver.completed == 1


def test_poison_message_is_dead_lettered():
    create()
    incoming = message(envelope(rows()[0]), body=[b'not JSON'])
    receiver = Receiver()
    assert handle_message(receiver, incoming, 'unit-test') == 'dead_letter'
    assert receiver.dead_lettered == 1 and receiver.completed == 0
    assert snapshot()['metrics']['observed_dead_letters_total'] == 1


def test_exhausted_processing_retries_are_dead_lettered(monkeypatch):
    create()
    incoming = message(envelope(rows()[0]), delivery_count=4)
    def unavailable(event):
        raise OperationalError('Simulated database outage')
    monkeypatch.setattr('trafficxx.consumer.process_event', unavailable)
    receiver = Receiver()
    assert handle_message(receiver, incoming, 'unit-test') == 'dead_letter'
    assert receiver.dead_lettered == 1
    assert snapshot()['metrics']['processing_failures_total'] == 1


def test_transient_processing_failure_is_abandoned_then_succeeds(monkeypatch):
    create()
    incoming = message(envelope(rows()[0]))
    receiver = Receiver()
    def unavailable(event):
        raise OperationalError('Simulated database outage')
    with monkeypatch.context() as patch:
        patch.setattr('trafficxx.consumer.process_event', unavailable)
        patch.setattr('trafficxx.consumer.time.sleep', lambda _: None)
        assert handle_message(receiver, incoming, 'unit-test') == 'retry'
    assert receiver.abandoned == 1 and receiver.completed == 0
    assert handle_message(receiver, incoming, 'unit-test') == 'processed'


def test_backlog_flags_age_and_clears_after_processing():
    create()
    with connect() as conn:
        conn.execute("UPDATE outbox SET created_at = clock_timestamp() - interval '1 minute'")
    assert 'OUTBOX_BACKLOG_AGING' in snapshot()['alerts']
    sent = []
    publish_one(sent.append)
    assert snapshot()['metrics']['processing_outstanding'] == 1
    assert 'PROCESSING_BACKLOG_AGING' in snapshot()['alerts']
    process_event(sent[0])
    status = snapshot()
    assert status['metrics']['outbox_pending'] == status['metrics']['processing_outstanding'] == 0
    assert status['alerts'] == []
    with TestClient(app) as client:
        assert client.get('/backlog').json()['metrics']['processed_total'] == 1
        assert 'trafficxx_outbox_pending 0' in client.get('/metrics').text


def test_replay_excludes_processed_and_known_dead_letters_and_preserves_ids():
    for _ in range(3):
        create()
    sent = []
    while publish_one(sent.append) != 'idle':
        pass
    process_event(sent[0])
    record_dead_letter('unit-test', message(sent[1]), 'PROCESSING_RETRIES_EXHAUSTED')
    assert replay_stalled() == 0
    with connect() as conn:
        conn.execute("UPDATE outbox SET published_at = clock_timestamp() - interval '2 minutes'")
    assert replay_stalled() == 1
    assert all(row['published_at'] is not None for row in rows())  # Dry run.
    assert replay_stalled(apply=True) == 1
    resent = []
    assert publish_one(resent.append) == 'published'
    assert resent == [sent[2]]
    assert process_event(resent[0])
    assert process_event(sent[2]) is False  # Original in-flight copy is harmless.


def test_migration_rerun_preserves_existing_data_and_message_id():
    hold = create()
    original = rows()[0]['message_id']
    run()
    run()
    assert rows()[0]['message_id'] == original
    assert reservation('alice', hold['id'])['status'] == 'HELD'


@pytest.mark.parametrize('change', [{'schema_version': 99}, {'unexpected': True}, {'occurred_at': 'bad-date'}])
def test_invalid_envelopes_are_rejected(change):
    create()
    event = envelope(rows()[0])
    with pytest.raises(InvalidEvent):
        parse_event(json.dumps({**event.model_dump(mode='json'), **change}), event.message_id)


def test_broker_message_id_must_match_envelope():
    create()
    event = envelope(rows()[0])
    with pytest.raises(InvalidEvent, match='MESSAGE_ID_MISMATCH'):
        parse_event(event.model_dump_json(), str(uuid4()))
