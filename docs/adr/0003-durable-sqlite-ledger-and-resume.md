# 0003. Durable SQLite ledger with forward-only resume

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

A run performs irreversible writes across four applications. A crash between two
writes leaves the world inconsistent, and a naive retry duplicates issues,
branches, and comments.

## Decision drivers

- A crash at any step must be recoverable without duplicate writes
- Evaluation numbers must come from one trustworthy source
- No external infrastructure for a single-operator deployment

## Considered options

1. In-memory state with a JSONL log
2. Postgres with a workflow engine such as Temporal
3. SQLite in WAL mode with an append-only event ledger

## Decision

Option 3. Every step writes `step_started` before acting and `step_finished`
after, in WAL mode. Resume replays the ledger, finds the last unfinished step, and
re-enters it. Every outbound write carries a `proofpr-run:<id>` marker that is
searched for before writing, so re-entry is idempotent.

## Consequences

**Good:**

- Crash and SIGKILL recovery is testable, and it is tested at every step
- The ledger is the single source of truth for the report generator
- Zero operational dependencies; the database is one file

**Bad:**

- Single-writer; concurrency across runs is process-level, not row-level
- Forward-only migrations must be written by hand

**Neutral:**

- A heavier engine remains a drop-in later because steps already persist their own state

## Rejected options and why

### In-memory state with a log

Cannot resume, and log parsing as a metrics source is fragile enough to quietly
corrupt results.

### Postgres plus a workflow engine

Correct, and disproportionate. It adds infrastructure that a solo deployment must
then operate, for durability guarantees SQLite already provides at this scale.
