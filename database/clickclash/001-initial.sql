CREATE SCHEMA clickclash;
REVOKE ALL ON SCHEMA clickclash FROM PUBLIC;
CREATE TABLE clickclash.players (
    id uuid PRIMARY KEY,
    email text UNIQUE,
    password_hash text,
    nickname text NOT NULL CHECK (length(nickname) BETWEEN 2 AND 32),
    bio text NOT NULL DEFAULT '' CHECK (length(bio) <= 180),
    website text NOT NULL DEFAULT '' CHECK (length(website) <= 500),
    score bigint NOT NULL DEFAULT 0 CHECK (score >= 0),
    merged_into uuid REFERENCES clickclash.players(id),
    ad_hidden boolean NOT NULL DEFAULT false,
    banned boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((email IS NULL) = (password_hash IS NULL))
);
CREATE INDEX players_ranking ON clickclash.players(score DESC, created_at, id)
    WHERE email IS NOT NULL AND merged_into IS NULL AND NOT banned;
CREATE TABLE clickclash.sessions (
    token_hash text PRIMARY KEY,
    player_id uuid NOT NULL REFERENCES clickclash.players(id),
    csrf text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_expiry ON clickclash.sessions(expires_at);
CREATE TABLE clickclash.receipts (
    id uuid PRIMARY KEY,
    player_id uuid NOT NULL REFERENCES clickclash.players(id),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX receipts_player ON clickclash.receipts(player_id);
CREATE TABLE clickclash.images (
    player_id uuid PRIMARY KEY REFERENCES clickclash.players(id),
    content bytea NOT NULL CHECK (octet_length(content) <= 524288),
    digest text NOT NULL
);
CREATE TABLE clickclash.control (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    paused boolean NOT NULL DEFAULT false,
    requests_per_second integer NOT NULL DEFAULT 120 CHECK (requests_per_second BETWEEN 1 AND 1000),
    concurrent_writes integer NOT NULL DEFAULT 16 CHECK (concurrent_writes BETWEEN 1 AND 32),
    image_bytes bigint NOT NULL DEFAULT 0 CHECK (image_bytes BETWEEN 0 AND 104857600)
);
INSERT INTO clickclash.control(id) VALUES (true);
CREATE TABLE clickclash.buckets (
    key text NOT NULL,
    bucket_start bigint NOT NULL,
    count integer NOT NULL,
    PRIMARY KEY (key, bucket_start)
);
CREATE TABLE clickclash.reports (
    reporter uuid NOT NULL REFERENCES clickclash.players(id),
    target uuid NOT NULL REFERENCES clickclash.players(id),
    reason text NOT NULL CHECK (length(reason) BETWEEN 3 AND 300),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (reporter, target)
);
CREATE TABLE clickclash.operator_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action text NOT NULL,
    target uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
