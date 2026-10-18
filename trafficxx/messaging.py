"""Transactional publishing and consumer effects, independent of broker transport."""
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from trafficxx.db import connect


class PublishError(Exception):
    """A retryable transport failure; never include credentials in its text."""


class InvalidEvent(Exception):
    pass


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    message_id: UUID
    outbox_id: int = Field(gt=0, strict=True)
    reservation_id: UUID
    event_id: str = Field(min_length=1, max_length=100)
    status: Literal['HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED']
    occurred_at: AwareDatetime


def envelope(row):
    return Event(schema_version=1, message_id=row['message_id'], outbox_id=row['id'],
                 reservation_id=row['reservation_id'], event_id=row['payload']['event_id'],
                 status=row['event_type'], occurred_at=row['created_at'])


def mark_published(conn, outbox_id):
    conn.execute('UPDATE outbox SET published_at = clock_timestamp(), last_error = NULL WHERE id = %s',
                 (outbox_id,))


def publish_one(send):
    """Hold one row lock through send/mark; a crash can resend the same message UUID."""
    with connect() as conn:
        row = conn.execute('SELECT * FROM outbox WHERE published_at IS NULL '
                           'AND next_attempt_at <= clock_timestamp() ORDER BY id '
                           'LIMIT 1 FOR UPDATE SKIP LOCKED').fetchone()
        if row is None:
            return 'idle'
        conn.execute('UPDATE outbox SET publish_attempts = publish_attempts + 1, '
                     'last_attempt_at = clock_timestamp() WHERE id = %s', (row['id'],))
        try:
            send(envelope(row))
        except PublishError:
            delay = min(60, 2 ** min(row['publish_failures'] + 1, 6))
            conn.execute('UPDATE outbox SET publish_failures = publish_failures + 1, '
                         "last_error = 'BROKER_SEND_FAILED', next_attempt_at = clock_timestamp() "
                         "+ %s * interval '1 second' WHERE id = %s", (delay, row['id']))
            return 'retry'
        mark_published(conn, row['id'])
        return 'published'


def parse_event(body, broker_message_id):
    try:
        event = Event.model_validate_json(body)
    except (ValidationError, ValueError, TypeError):
        raise InvalidEvent('INVALID_EVENT_SCHEMA') from None
    if str(event.message_id) != str(broker_message_id):
        raise InvalidEvent('MESSAGE_ID_MISMATCH')
    return event


def apply_projection(conn, event):
    conn.execute(
        'INSERT INTO reservation_updates(reservation_id, event_id, status, last_outbox_id) '
        'VALUES (%s, %s, %s, %s) ON CONFLICT (reservation_id) DO UPDATE '
        'SET event_id = EXCLUDED.event_id, status = EXCLUDED.status, '
        'last_outbox_id = EXCLUDED.last_outbox_id, updated_at = clock_timestamp() '
        'WHERE reservation_updates.last_outbox_id < EXCLUDED.last_outbox_id',
        (event.reservation_id, event.event_id, event.status, event.outbox_id),
    )


def process_event(event):
    """Commit deduplication and the downstream effect together before broker acknowledgement."""
    payload = event.model_dump(mode='json')
    with connect() as conn:
        inserted = conn.execute(
            'INSERT INTO consumer_inbox(message_id, envelope) VALUES (%s, %s) '
            'ON CONFLICT DO NOTHING RETURNING message_id', (event.message_id, Jsonb(payload)),
        ).fetchone()
        if inserted is None:
            previous = conn.execute('SELECT envelope FROM consumer_inbox WHERE message_id = %s',
                                    (event.message_id,)).fetchone()
            if previous['envelope'] != payload:
                raise InvalidEvent('MESSAGE_ID_REUSED')
            return False
        apply_projection(conn, event)
        return True


def heartbeat(name, error=None):
    with connect() as conn:
        conn.execute('INSERT INTO worker_heartbeats(name, last_error) VALUES (%s, %s) '
                     'ON CONFLICT (name) DO UPDATE SET last_seen_at = clock_timestamp(), '
                     'last_error = EXCLUDED.last_error', (name, error))


def record_failure(message_id, error_code):
    with connect() as conn:
        conn.execute('INSERT INTO worker_failures(message_id, error_code) VALUES (%s, %s)',
                     (str(message_id)[:200], error_code))


def record_dead_letter(queue, message, reason=None):
    with connect() as conn:
        conn.execute('INSERT INTO observed_dead_letters(queue_name, sequence_number, message_id, reason) '
                     'VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
                     (queue, message.sequence_number, str(message.message_id)[:200],
                      str(reason or message.dead_letter_reason or 'UNKNOWN')[:200]))


def replay_stalled(older_than_seconds=60, apply=False):
    if older_than_seconds < 10:
        raise ValueError('Replay grace period must be at least ten seconds')
    with connect() as conn:
        rows = conn.execute(
            'SELECT o.id FROM outbox o WHERE o.published_at IS NOT NULL '
            "AND o.published_at < clock_timestamp() - %s * interval '1 second' "
            'AND NOT EXISTS (SELECT 1 FROM consumer_inbox i WHERE i.message_id = o.message_id) '
            'AND NOT EXISTS (SELECT 1 FROM observed_dead_letters d WHERE d.message_id = o.message_id::text) '
            'ORDER BY o.id FOR UPDATE OF o SKIP LOCKED', (older_than_seconds,),
        ).fetchall()
        if apply and rows:
            conn.execute('UPDATE outbox SET published_at = NULL, next_attempt_at = clock_timestamp() '
                         'WHERE id = ANY(%s)', ([row['id'] for row in rows],))
        return len(rows)
