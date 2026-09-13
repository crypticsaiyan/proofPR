"""Durable append-only ledger in SQLite.

Three properties matter, in this order:

1. **Resume.** Every step writes ``step_started`` before acting and
   ``step_finished`` after. A run that died mid-step is the run whose last event
   for that step is ``step_started``.
2. **Evidence.** Evaluation numbers are computed from these rows and nothing
   else. Log parsing is not a fallback.
3. **Tamper evidence.** Each event extends a hash chain,
   ``h_n = sha256(h_{n-1} || canonical(event_n))``. The final digest is the
   receipt published in the pull request, the Linear issue, and the thread.

WAL mode, ``synchronous=FULL``. Durability is worth more than throughput here:
losing the record of a write that already happened is the one failure this
design cannot recover from.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from opentelemetry import trace
from opentelemetry.trace import Span

from proofpr.domain.enums import Arm, Outcome, Reason, Step
from proofpr.domain.errors import ProofPRError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.observability.tracing import get_tracer

#: Migrations ship inside the package. Resolving them from the working directory
#: worked only when the process happened to start in the repository root, which
#: made `proofpr` unusable from anywhere else and made every test that changed
#: directory pass a path by hand.
MIGRATIONS_DIR = Path(__file__).parent / "migrations"

#: The chain's starting value. Fixed and published so a verifier needs nothing
#: from us except the events themselves.
GENESIS_HASH = "0" * 64


class LedgerError(ProofPRError):
    """The ledger could not be opened, migrated, or appended to."""

    code = "ledger_error"


def _now() -> str:
    """Return an ISO 8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def canonical(payload: Mapping[str, Any]) -> str:
    """Serialise a payload deterministically, for hashing.

    Sorted keys and no insignificant whitespace, so the same event always
    produces the same digest regardless of how it was constructed.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def chain(previous: str, payload: Mapping[str, Any]) -> str:
    """Return the next hash in the chain."""
    digest = hashlib.sha256()
    digest.update(previous.encode("utf-8"))
    digest.update(canonical(payload).encode("utf-8"))
    return digest.hexdigest()


def new_run_id() -> str:
    """Return a short, human-quotable run identifier such as ``r-7f3a``."""
    return f"r-{uuid.uuid4().hex[:4]}"


class Ledger:
    """A SQLite-backed append-only run ledger."""

    def __init__(self, path: Path, *, migrations_dir: Path | None = None) -> None:
        """Open, creating the file and applying migrations if needed."""
        self.path = path
        self._migrations_dir = migrations_dir or MIGRATIONS_DIR
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()
        # One root span per run, alive between `start_run` and `finish_run`,
        # and one child span per step, alive between `step_started` and
        # `step_finished`/`step_skipped`. Kept here rather than passed around
        # because every event a step produces already flows through `append`,
        # which makes this the one place a span's lifetime is guaranteed to
        # match the ledger row it was opened for. Spans are no-ops unless
        # `configure_tracing` installed a real exporter.
        self._run_spans: dict[str, Span] = {}
        self._step_spans: dict[tuple[str, str], Span] = {}

    def _configure(self) -> None:
        """Apply the pragmas that make this durable rather than merely fast."""
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def __enter__(self) -> Self:
        """Return self, for use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection."""
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # -- schema ---------------------------------------------------------------

    def migrate(self) -> list[str]:
        """Apply any unapplied migrations, in filename order.

        Returns:
            The names of the migrations applied by this call.

        Raises:
            LedgerError: A migration file failed to apply.
        """
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row["name"] for row in self._conn.execute("SELECT name FROM schema_migrations")}
        pending = sorted(
            path for path in self._migrations_dir.glob("*.sql") if path.name not in applied
        )
        for path in pending:
            try:
                self._conn.executescript(path.read_text(encoding="utf-8"))
            except sqlite3.Error as error:
                raise LedgerError(f"migration {path.name} failed: {error}") from error
            self._conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (path.name, _now()),
            )
        return [path.name for path in pending]

    # -- runs -----------------------------------------------------------------

    def start_run(
        self,
        *,
        source: str,
        arm: Arm = Arm.PROOFPR,
        run_id: str | None = None,
        prompt_version: str | None = None,
    ) -> str:
        """Record the start of a run and return its identifier."""
        identifier = run_id or new_run_id()
        self._conn.execute(
            "INSERT INTO runs(run_id, arm, source, started_at, prompt_version)"
            " VALUES (?, ?, ?, ?, ?)",
            (identifier, arm.value, source, _now(), prompt_version),
        )
        span = get_tracer().start_span("proofpr.run")
        span.set_attribute("proofpr.run_id", identifier)
        span.set_attribute("proofpr.arm", arm.value)
        self._run_spans[identifier] = span
        self.append(identifier, "run_started", {"source": source, "arm": arm.value})
        return identifier

    def finish_run(
        self,
        run_id: str,
        *,
        outcome: Outcome,
        reason: Reason | None = None,
        receipt: str | None = None,
    ) -> str:
        """Record a terminal state and seal the run with its receipt.

        The receipt is a chain hash from **before** the closing event, so it can
        be published inside that event and inside the pull request, the issue,
        and the thread. A receipt that had to include the event announcing
        itself could never be printed anywhere the run could still write to.

        By default the receipt is the current chain head, which suits a run that
        publishes nothing (there is no earlier value anyone has already seen).
        A run that published a receipt mid-run passes that exact value here, so
        the run is sealed with the same digest a reviewer already has, rather
        than a fresh one computed after further events (the publishing write
        itself, the final state snapshot) extended the chain further.

        Returns:
            The receipt.
        """
        sealed = receipt if receipt is not None else self.head_hash(run_id)
        self.append(
            run_id,
            "run_finished",
            {
                "outcome": outcome.value,
                "reason": reason.value if reason else None,
                "receipt": sealed,
            },
        )
        self._conn.execute(
            "UPDATE runs SET finished_at = ?, outcome = ?, reason = ?, receipt = ?"
            " WHERE run_id = ?",
            (_now(), outcome.value, reason.value if reason else None, sealed, run_id),
        )
        span = self._run_spans.pop(run_id, None)
        if span is not None:
            span.set_attribute("proofpr.outcome", outcome.value)
            if reason is not None:
                span.set_attribute("proofpr.reason", reason.value)
            span.end()
        return sealed

    def verify_receipt(self, run_id: str) -> tuple[bool, str]:
        """Recompute the chain and compare it to the published receipt.

        Returns:
            Whether the run verifies, and a sentence saying why not when it does
            not. A run with no receipt is unverifiable rather than invalid: it
            never finished.
        """
        row = self.get_run(run_id)
        if row is None:
            return False, f"no run {run_id} in this ledger"

        intact, broken_at = self.verify_chain(run_id)
        if not intact:
            return False, f"the chain is broken at event {broken_at}"

        published = row.get("receipt")
        if not published:
            return False, "this run has no receipt: it never reached a terminal state"

        events = self.events(run_id)
        if not events or events[-1]["kind"] != "run_finished":
            return False, "the run has a receipt but no closing event"

        # The receipt need not be the immediately preceding event: a run that
        # published its receipt mid-run seals with that same, earlier value, and
        # everything recorded after it (the publishing write, the final
        # snapshot) still extends the same intact chain. What matters is that
        # the published value is *some* real position in a chain proven intact,
        # which nobody could produce without having genuinely recomputed every
        # canonical payload before it.
        chain_hashes = {str(event["chain_hash"]) for event in events[:-1]}
        if not chain_hashes:
            chain_hashes = {GENESIS_HASH}
        if published not in chain_hashes:
            return False, "the published receipt does not match the recomputed chain"

        inside = events[-1]["payload"].get("receipt")
        if inside != published:
            return False, "the closing event carries a different receipt"

        return True, f"{len(events)} events verify against receipt sha256:{published[:16]}"

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run row, or None."""
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def finished_runs(self, *, limit: int = 50, max_age_days: int = 14) -> list[dict[str, Any]]:
        """Return recently finished runs, newest first.

        Bounded by age as well as count, because reconciling a month-old run
        would post into threads nobody is reading any more.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL AND finished_at >= ?"
            " ORDER BY finished_at DESC LIMIT ?",
            (cutoff, limit),
        )
        return [dict(row) for row in rows]

    def unfinished_runs(self) -> list[dict[str, Any]]:
        """Return runs that never reached a terminal state, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NULL ORDER BY started_at"
        )
        return [dict(row) for row in rows]

    # -- events ---------------------------------------------------------------

    def append(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        step: Step | None = None,
    ) -> str:
        """Append one event and extend the hash chain.

        Args:
            run_id: The run the event belongs to.
            kind: Event kind, for example ``step_started`` or ``guard_blocked``.
            payload: Event data. Must be JSON serialisable.
            step: The step this event belongs to, when applicable.

        Returns:
            The new chain head.
        """
        body = dict(payload or {})
        at = _now()
        record = {
            "run_id": run_id,
            "at": at,
            "kind": kind,
            "step": step.value if step else None,
            "payload": body,
        }
        head = chain(self.head_hash(run_id), record)
        self._conn.execute(
            "INSERT INTO events(run_id, at, kind, step, payload, chain_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, at, kind, step.value if step else None, canonical(body), head),
        )
        if step is not None:
            self._trace_step(run_id, step, kind)
        return head

    def _trace_step(self, run_id: str, step: Step, kind: str) -> None:
        """Open or close this step's span, keyed by the event that names it.

        A resumed run has no root span in this process (spans do not survive a
        restart), so a step traced during resume becomes its own root instead
        of a child — a real trace either way, just not nested under one that no
        longer exists here.
        """
        key = (run_id, step.value)
        if kind == "step_started":
            parent = self._run_spans.get(run_id)
            context = trace.set_span_in_context(parent) if parent else None
            span = get_tracer().start_span(f"step.{step.value}", context=context)
            self._step_spans[key] = span
        elif kind in ("step_finished", "step_skipped") and key in self._step_spans:
            span = self._step_spans.pop(key)
            span.set_attribute("proofpr.event", kind)
            span.end()

    def events(self, run_id: str) -> list[dict[str, Any]]:
        """Return every event for a run, in order."""
        rows = self._conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def head_hash(self, run_id: str) -> str:
        """Return the current chain head for a run, or the genesis value."""
        row = self._conn.execute(
            "SELECT chain_hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return str(row["chain_hash"]) if row else GENESIS_HASH

    def verify_chain(self, run_id: str) -> tuple[bool, int | None]:
        """Recompute a run's chain and compare it to what is stored.

        Returns:
            ``(True, None)`` when intact, otherwise ``(False, seq)`` naming the
            first event whose stored hash does not match.
        """
        previous = GENESIS_HASH
        for row in self.events(run_id):
            record = {
                "run_id": row["run_id"],
                "at": row["at"],
                "kind": row["kind"],
                "step": row["step"],
                "payload": row["payload"],
            }
            expected = chain(previous, record)
            if expected != row["chain_hash"]:
                return False, int(row["seq"])
            previous = expected
        return True, None

    def resume_point(self, run_id: str) -> Step | None:
        """Return the step a crashed run died inside, if any.

        A step is unfinished when its most recent event is ``step_started``.
        """
        started: Step | None = None
        for row in self.events(run_id):
            if row["kind"] == "step_started" and row["step"]:
                started = Step(row["step"])
            elif row["kind"] == "step_finished" and started and row["step"] == started.value:
                started = None
        return started

    # -- writes ---------------------------------------------------------------

    def record_attempt(self, intent: WriteIntent) -> None:
        """Record that a write is about to be sent, before sending it.

        A crash between sending and recording would otherwise leave no trace of
        the attempt, and resume would send it again without first checking
        whether the application already holds it. An existing record is left
        untouched, so an attempt never erases a verified write.
        """
        self._conn.execute(
            "INSERT INTO writes(run_id, app, operation, target, remote_id, url, verified, at)"
            " VALUES (?, ?, ?, ?, NULL, NULL, 0, ?)"
            " ON CONFLICT(run_id, operation, target) DO NOTHING",
            (intent.run_id, intent.app, intent.operation, intent.target, _now()),
        )

    def record_write(self, result: WriteResult) -> None:
        """Record an attempted or verified write, keyed for idempotent retry."""
        intent = result.intent
        self._conn.execute(
            "INSERT INTO writes(run_id, app, operation, target, remote_id, url, verified, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id, operation, target) DO UPDATE SET"
            " remote_id = excluded.remote_id, url = excluded.url,"
            " verified = excluded.verified, at = excluded.at",
            (
                intent.run_id,
                intent.app,
                intent.operation,
                intent.target,
                result.remote_id,
                result.url,
                int(result.verified),
                _now(),
            ),
        )

    def writes(self, run_id: str) -> list[dict[str, Any]]:
        """Return every write a run attempted, oldest first."""
        rows = self._conn.execute("SELECT * FROM writes WHERE run_id = ? ORDER BY at", (run_id,))
        return [dict(row) for row in rows]

    def find_write(self, intent: WriteIntent) -> dict[str, Any] | None:
        """Return a previous attempt at the same write, if there was one.

        This is how a retry after a crash avoids creating a second issue: the
        ledger is consulted before the remote application is searched.
        """
        row = self._conn.execute(
            "SELECT * FROM writes WHERE run_id = ? AND operation = ? AND target = ?",
            (intent.run_id, intent.operation, intent.target),
        ).fetchone()
        return dict(row) if row else None

    # -- model calls ----------------------------------------------------------

    # -- annotations ----------------------------------------------------------

    def annotate(self, run_id: str, key: str, value: Any) -> None:  # noqa: ANN401
        """Record a judgement made about a run after it finished.

        Outside the hash chain on purpose. The receipt covers what the agent
        did; a hidden maintainer test applied afterwards is the harness's
        verdict on a sealed run, and appending it as an event would break the
        very receipt it accompanies. Last write for a key wins, because a
        re-judgement replaces a judgement rather than adding one.
        """
        self._conn.execute(
            "INSERT INTO annotations(run_id, key, value, at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(run_id, key) DO UPDATE SET value = excluded.value, at = excluded.at",
            (run_id, key, json.dumps(value, default=str), _now()),
        )

    def annotations(self, run_id: str) -> dict[str, Any]:
        """Return every annotation on a run, decoded."""
        rows = self._conn.execute("SELECT key, value FROM annotations WHERE run_id = ?", (run_id,))
        return {str(row["key"]): json.loads(row["value"]) for row in rows}

    def runs_with_annotation(self, key: str) -> list[str]:
        """Return every run carrying an annotation, oldest first."""
        rows = self._conn.execute(
            "SELECT a.run_id FROM annotations a JOIN runs r ON r.run_id = a.run_id"
            " WHERE a.key = ? ORDER BY r.started_at",
            (key,),
        )
        return [str(row["run_id"]) for row in rows]

    def annotation(self, run_id: str, key: str) -> Any:  # noqa: ANN401
        """Return one annotation, or None when the run carries no such key."""
        row = self._conn.execute(
            "SELECT value FROM annotations WHERE run_id = ? AND key = ?", (run_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def record_model_call(
        self,
        run_id: str,
        *,
        step: Step,
        tier: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Record usage and cost for one model call."""
        self._conn.execute(
            "INSERT INTO model_calls(run_id, at, step, tier, model, input_tokens,"
            " output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, _now(), step.value, tier, model, input_tokens, output_tokens, cost_usd),
        )

    def models_used(self) -> set[str]:
        """Return every model name that answered a call in this ledger."""
        rows = self._conn.execute("SELECT DISTINCT model FROM model_calls")
        return {str(row["model"]) for row in rows}

    def total_cost(self, run_id: str | None = None) -> float:
        """Return spend for one run, or for every run when ``run_id`` is None."""
        if run_id is None:
            row = self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM model_calls")
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM model_calls WHERE run_id = ?",
                (run_id,),
            )
        return float(row.fetchone()["total"])


def open_ledger(path: Path, *, migrations_dir: Path | None = None) -> Iterator[Ledger]:
    """Yield an open ledger and close it afterwards."""
    with closing(Ledger(path, migrations_dir=migrations_dir)) as ledger:
        yield ledger
