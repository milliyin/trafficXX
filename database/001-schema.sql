CREATE TABLE events (
    id text PRIMARY KEY,
    name text NOT NULL,
    inventory_type text NOT NULL CHECK (inventory_type IN ('GENERAL', 'SEATED'))
);

CREATE TABLE reservations (
    id uuid PRIMARY KEY,
    event_id text NOT NULL REFERENCES events(id),
    customer_id text NOT NULL,
    idempotency_key text NOT NULL,
    request jsonb NOT NULL,
    quantity integer NOT NULL CHECK (quantity > 0),
    status text NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    UNIQUE (customer_id, idempotency_key),
    UNIQUE (id, event_id)
);
CREATE INDEX due_holds ON reservations(event_id, expires_at) WHERE status = 'HELD';

-- General admission uses anonymous units; assigned seating uses labelled units.
-- A unit can reference only one reservation, including after confirmation.
CREATE TABLE ticket_units (
    event_id text NOT NULL REFERENCES events(id),
    unit_number integer NOT NULL,
    seat_label text,
    reservation_id uuid,
    PRIMARY KEY (event_id, unit_number),
    UNIQUE (event_id, seat_label),
    FOREIGN KEY (reservation_id, event_id) REFERENCES reservations(id, event_id)
);
CREATE INDEX free_units ON ticket_units(event_id, unit_number) WHERE reservation_id IS NULL;

CREATE TABLE outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reservation_id uuid NOT NULL REFERENCES reservations(id),
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    UNIQUE (reservation_id, event_type)
);
CREATE INDEX pending_outbox ON outbox(created_at) WHERE published_at IS NULL;
