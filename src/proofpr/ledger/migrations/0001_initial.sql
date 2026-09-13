-- Initial ledger schema.
--
-- Append-only. There is no UPDATE and no DELETE anywhere in the application
-- against these tables; the hash chain in `events.chain_hash` would detect one,
-- and the receipt would stop verifying.

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    arm           TEXT NOT NULL,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT,
    reason        TEXT,
    receipt       TEXT,
    prompt_version TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    step       TEXT,
    payload    TEXT NOT NULL,
    chain_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_run_idx  ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind);

-- One row per attempted write, so readback state survives a crash and a retry
-- can find its own earlier attempt without searching the remote application.
CREATE TABLE IF NOT EXISTS writes (
    run_id     TEXT NOT NULL REFERENCES runs(run_id),
    app        TEXT NOT NULL,
    operation  TEXT NOT NULL,
    target     TEXT NOT NULL,
    remote_id  TEXT,
    url        TEXT,
    verified   INTEGER NOT NULL DEFAULT 0,
    at         TEXT NOT NULL,
    PRIMARY KEY (run_id, operation, target)
);

CREATE TABLE IF NOT EXISTS model_calls (
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    at            TEXT NOT NULL,
    step          TEXT NOT NULL,
    tier          TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS model_calls_run_idx ON model_calls(run_id);
