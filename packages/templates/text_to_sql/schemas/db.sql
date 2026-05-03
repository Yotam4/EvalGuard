-- Shadow-DB bootstrap for the text_to_sql template demo.
-- This DDL is executed against the configured ``systems[shadow]`` URL
-- before any candidate or expected SQL runs. Keep it self-contained
-- (no extension installs) and small enough to seed in milliseconds —
-- the same DDL is re-applied at the start of every row evaluation.

CREATE TABLE IF NOT EXISTS customers (
    id          INTEGER PRIMARY KEY,
    signup_date TEXT    NOT NULL,         -- ISO-8601 date
    country     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    amount      REAL    NOT NULL,
    created_at  TEXT    NOT NULL
);

-- Seed a tiny, deterministic dataset. The text_to_sql golden set's
-- expected_result entries assume exactly this seed.

INSERT OR IGNORE INTO customers (id, signup_date, country) VALUES
    (1, '2024-02-10', 'US'),
    (2, '2024-05-22', 'US'),
    (3, '2024-09-01', 'DE'),
    (4, '2023-11-30', 'US'),
    (5, '2024-12-15', 'FR');

INSERT OR IGNORE INTO products (id, name) VALUES
    (1, 'Widget'),
    (2, 'Gadget'),
    (3, 'Sprocket');

INSERT OR IGNORE INTO orders (id, product_id, amount, created_at) VALUES
    (1, 1, 100.00, '2024-01-15'),
    (2, 1,  50.00, '2024-01-20'),
    (3, 2, 250.00, '2024-01-22'),
    (4, 3,  10.00, '2024-01-28'),
    (5, 2, 175.00, '2024-02-03');
