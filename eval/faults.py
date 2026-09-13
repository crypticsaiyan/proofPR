"""The fault evaluation: one planned failure per cell, and what survives it.

Each cell runs the publishable case with exactly one failure injected on one
write, lets the pipeline recover however it can (retry, lookup, pause, resume),
and then counts what the applications actually hold. The count, not the
pipeline's own opinion of itself, is the result.

Honesty rules, each enforced here rather than in the report:

- A cell whose fault never fired is `invalid`. The run did not reach the write,
  so it proves nothing about recovering from a failure on it.
- A write that went missing is a failure just as much as one that landed twice.
- Paused and crashed runs are resumed exactly once. A run that needs a second
  resume is reported as not recovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from arms import PROOFPR
from proofpr.domain.errors import RetryableAppError
from proofpr.domain.run import Report
from proofpr.observability.logging import get_logger
from runner import MemoryApps, Runner
from schema import Case

logger = get_logger("proofpr.eval.faults")

#: The label every fault run carries as its source, so arm summaries and the
#: paired comparison never mix fault runs into case results.
FAULT_SOURCE = "faults"


class ProcessDied(Exception):  # noqa: N818 - models a crash, not an error condition
    """A simulated process death. Deliberately not a ProofPR error."""


@dataclass(frozen=True, slots=True)
class FaultMode:
    """One way a write can fail."""

    name: str
    error: str | int
    times: int
    after_apply: bool

    def exception(self, operation: str) -> Exception:
        """Build the exception this mode raises for an operation."""
        app = operation.split(".", 1)[0]
        if self.error == "crash":
            return ProcessDied(f"process died after {operation}")
        if self.error == "timeout":
            return RetryableAppError(f"injected timeout on {operation}", app=app)
        return RetryableAppError(
            f"injected {self.error} on {operation}", app=app, status=int(self.error)
        )


@dataclass(frozen=True, slots=True)
class FaultMatrix:
    """Every cell the fault evaluation runs."""

    case_id: str
    operations: tuple[str, ...]
    modes: tuple[FaultMode, ...]
    skip: frozenset[tuple[str, str]]

    @classmethod
    def load(cls, path: Path) -> FaultMatrix:
        """Load the matrix from YAML."""
        document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            case_id=str(document["case"]),
            operations=tuple(str(op) for op in document["operations"]),
            modes=tuple(
                FaultMode(
                    name=str(name),
                    error=spec["error"],
                    times=int(spec.get("times", 1)),
                    after_apply=bool(spec.get("after_apply", False)),
                )
                for name, spec in document["modes"].items()
            ),
            skip=frozenset((str(op), str(mode)) for op, mode in document.get("skip", [])),
        )

    def cells(self) -> list[tuple[str, FaultMode]]:
        """Return every (operation, mode) pair that is not skipped."""
        return [
            (operation, mode)
            for operation in self.operations
            for mode in self.modes
            if (operation, mode.name) not in self.skip
        ]


@dataclass
class FaultResult:
    """One cell."""

    operation: str
    mode: str
    run_id: str = ""
    fired: bool = False
    resumed: bool = False
    outcome: str = "none"
    consistent: bool | None = None
    duplicates: int = 0
    missing: int = 0
    receipt_verified: bool = False
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        """Whether the fault actually fired, so the cell measured something."""
        return self.fired

    @property
    def recovered(self) -> bool:
        """Finished as intended, consistent, nothing twice, nothing missing."""
        return (
            self.valid
            and self.outcome == "pr_opened"
            and self.consistent is True
            and self.duplicates == 0
            and self.missing == 0
            and self.receipt_verified
        )


def count_objects(apps: MemoryApps) -> dict[str, int]:
    """Count what a successful run is supposed to leave behind, one of each."""
    issue = next(iter(apps.linear.issues.values()), None)
    return {
        "linear_issue": len(apps.linear.issues),
        "pull_request": len(apps.github.pulls),
        "branch": len(apps.github.branches) - 1,
        "discord_reply": sum(
            1 for m in apps.discord.messages.values() if m.get("author", {}).get("id") == "bot"
        ),
        "attachment": len(issue["attachments"]["nodes"]) if issue else 0,
        "receipt_comment": (
            sum(1 for n in issue["comments"]["nodes"] if "Receipt" in n["body"]) if issue else 0
        ),
    }


class FaultRunner:
    """Runs the fault matrix through the real pipeline and records every cell."""

    def __init__(self, runner: Runner, matrix: FaultMatrix, case: Case) -> None:
        """Build over an existing case runner, so the pipeline is identical."""
        self.runner = runner
        self.matrix = matrix
        self.case = case

    async def run_all(self) -> list[FaultResult]:
        """Run every cell, in order."""
        results = []
        for operation, mode in self.matrix.cells():
            logger.info("fault_cell_started", operation=operation, mode=mode.name)
            results.append(await self.run_cell(operation, mode))
        return results

    async def run_cell(self, operation: str, mode: FaultMode) -> FaultResult:
        """Run one cell: inject, run, resume once if needed, then count."""
        ledger = self.runner.ledger
        apps = self.runner.build_apps(self.case)
        pipeline = self.runner.build_pipeline(apps, self.case, PROOFPR)
        fake = getattr(apps, operation.split(".", 1)[0])
        fake.inject(
            operation, mode.exception(operation), after_apply=mode.after_apply, times=mode.times
        )

        result = FaultResult(operation=operation, mode=mode.name)
        report = Report(
            source=FAULT_SOURCE,
            text=self.case.text,
            author=self.case.author,
            channel_id=1,
            message_id=1,
        )
        try:
            try:
                state = await pipeline.run(report)
            except ProcessDied:
                state = None
            result.fired = bool(fake.fired)

            run_id = state.run_id if state is not None else _newest_unfinished(ledger)
            result.run_id = run_id
            row = ledger.get_run(run_id)
            if row is not None and not row["finished_at"]:
                # Paused or crashed. The application comes back, and the run is
                # resumed exactly once.
                fake.faults.clear()
                state = await pipeline.resume(run_id)
                result.resumed = True

            assert state is not None  # noqa: S101 - resume always returns a state
            result.outcome = state.outcome.value if state.outcome else "none"
            result.consistent = state.consistent
            result.receipt_verified = ledger.verify_receipt(run_id)[0]
        except Exception as error:  # noqa: BLE001 - a failed cell is a result
            result.error = f"{type(error).__name__}: {error}"
            logger.warning(
                "fault_cell_failed", operation=operation, mode=mode.name, error=str(error)
            )

        counts = count_objects(apps)
        result.counts = counts
        result.duplicates = sum(max(0, n - 1) for n in counts.values())
        result.missing = sum(1 for n in counts.values() if n == 0)

        if result.run_id:
            ledger.annotate(
                result.run_id,
                "fault",
                {
                    "operation": operation,
                    "mode": mode.name,
                    "fired": result.fired,
                    "resumed": result.resumed,
                    "duplicates": result.duplicates,
                    "missing": result.missing,
                    "counts": counts,
                    "consistent": result.consistent,
                    "receipt_verified": result.receipt_verified,
                    "recovered": result.recovered,
                    "error": result.error,
                },
            )
        return result


def _newest_unfinished(ledger: Any) -> str:  # noqa: ANN401 - a Ledger
    """Return the most recently started unfinished run."""
    rows = ledger.unfinished_runs()
    return str(rows[-1]["run_id"]) if rows else ""


__all__ = ["FAULT_SOURCE", "FaultMatrix", "FaultMode", "FaultResult", "FaultRunner"]
