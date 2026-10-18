ALTER TABLE outbox
    ADD COLUMN message_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    ADD COLUMN publish_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN publish_failures integer NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN last_attempt_at timestamptz,
    ADD COLUMN last_error text;

CREATE TABLE consumer_inbox (
    message_id uuid PRIMARY KEY,
    envelope jsonb NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- A simulated downstream effect: a read model of the latest delivered state.
CREATE TABLE reservation_updates (
    reservation_id uuid PRIMARY KEY REFERENCES reservations(id),
    event_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    last_outbox_id bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE worker_failures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id text NOT NULL,
    error_code text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE observed_dead_letters (
    queue_name text NOT NULL,
    sequence_number bigint NOT NULL,
    message_id text NOT NULL,
    reason text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (queue_name, sequence_number)
);
CREATE INDEX dead_letter_message ON observed_dead_letters(message_id);

CREATE TABLE worker_heartbeats (
    name text PRIMARY KEY,
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error text
);
