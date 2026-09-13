-- Judgements made about a run after it finished.
--
-- Deliberately outside the hash chain. A run's receipt covers what the agent
-- did; the hidden maintainer test is applied by the harness afterwards, against
-- a sealed run, and appending it as an event would break the receipt it is
-- meant to accompany. These rows are the harness's own record, and nothing the
-- agent writes can reach them.

CREATE TABLE IF NOT EXISTS annotations (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    at     TEXT NOT NULL,
    PRIMARY KEY (run_id, key)
);

CREATE INDEX IF NOT EXISTS annotations_key_idx ON annotations(key);
