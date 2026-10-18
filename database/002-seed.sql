INSERT INTO events VALUES
    ('demo-general', 'Demo concert - general admission', 'GENERAL'),
    ('demo-seated', 'Demo concert - assigned seats', 'SEATED');
INSERT INTO ticket_units(event_id, unit_number)
SELECT 'demo-general', n FROM generate_series(1, 100) AS n;
INSERT INTO ticket_units(event_id, unit_number, seat_label)
SELECT 'demo-seated', n, 'A' || n FROM generate_series(1, 100) AS n;
