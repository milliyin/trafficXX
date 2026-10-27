CREATE TABLE clickclash.image_blobs (
    player_id uuid PRIMARY KEY REFERENCES clickclash.players(id),
    blob_name text NOT NULL UNIQUE,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 1 AND 524288)
);
