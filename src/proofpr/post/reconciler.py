"""The drift reconciler.

Verification records disagreement between Discord, GitHub, and Linear; this repairs it.
The two are deliberately separate. A repair that happens during verification
cannot be distinguished from a system that was correct in the first place, and
the difference is exactly what the evaluation measures.

What drifts, in practice: an issue left in triage after a pull request opened, a
reply that failed after the run had moved on, a pull request nobody linked. What
is never repaired: anything requiring a new decision. The reconciler re-applies
conclusions the run already reached, and
never reaches new ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proofpr.domain.enums import Outcome
from proofpr.domain.models import WriteIntent
from proofpr.domain.run import RunState
from proofpr.ledger import Ledger
from proofpr.observability.logging import get_logger
from proofpr.steps import verify as verify_step

logger = get_logger("proofpr.reconciler")

#: Runs older than this are left alone. A month-old divergence is history, and
#: repairing it would post to threads nobody is reading any more.
MAX_AGE_DAYS = 14


@dataclass
class Repair:
    """One divergence, and what was done about it."""

    run_id: str
    app: str
    problem: str
    repaired: bool
    detail: str = ""


@dataclass
class ReconcileReport:
    """What a reconciliation pass found."""

    checked: int = 0
    diverged: int = 0
    repairs: list[Repair] = field(default_factory=list)

    @property
    def repaired(self) -> int:
        """How many divergences were fixed."""
        return sum(1 for repair in self.repairs if repair.repaired)

    @property
    def unrepaired(self) -> list[Repair]:
        """Divergences left for a human."""
        return [repair for repair in self.repairs if not repair.repaired]


#: Events carrying a full state snapshot, newest first when scanning backwards.
SNAPSHOT_KINDS = frozenset({"state_final", "step_finished"})


def last_state(ledger: Ledger, run_id: str) -> RunState | None:
    """Rebuild the final state of a run from its last snapshot.

    `state_final` is written after the terminal writes, so it is the only
    snapshot that knows about the issue, the reply, and the attachment. Scanning
    backwards finds it first when it exists and falls back to the last completed
    step when the run died before reaching one.
    """
    for event in reversed(ledger.events(run_id)):
        if event["kind"] in SNAPSHOT_KINDS and event["payload"]:
            return RunState.model_validate(event["payload"])
    return None


class Reconciler:
    """Re-checks finished runs and repairs what it safely can."""

    def __init__(self, *, apps: Any, ledger: Ledger) -> None:  # noqa: ANN401
        """Build a reconciler over the applications and the ledger."""
        self.apps = apps
        self.ledger = ledger

    async def run_once(self, *, limit: int = 50) -> ReconcileReport:
        """Re-verify recent finished runs and repair the divergences found."""
        report = ReconcileReport()
        for row in self._recent(limit):
            state = last_state(self.ledger, str(row["run_id"]))
            if state is None:
                continue
            report.checked += 1
            outcome = Outcome(str(row["outcome"]))
            try:
                consistency = await self._check(state, outcome)
            except Exception as error:  # noqa: BLE001 - one unreachable app must not
                # abort the sweep. The other runs are still worth checking, and a
                # sweep that dies on the first outage repairs nothing at all.
                logger.warning("reconcile_check_failed", run_id=state.run_id, error=str(error))
                report.repairs.append(
                    Repair(
                        state.run_id,
                        "unknown",
                        f"could not be checked: {error}",
                        repaired=False,
                        detail="the applications could not be read",
                    )
                )
                continue
            if consistency.consistent:
                continue

            report.diverged += 1
            for app, ok in consistency.checks.items():
                if ok:
                    continue
                problem = next(
                    (p for p in consistency.problems if p.startswith(f"{app}:")), f"{app}: unknown"
                )
                repair = await self._repair(state, outcome, app, problem)
                report.repairs.append(repair)
                self.ledger.append(
                    state.run_id,
                    "reconciled",
                    {
                        "app": app,
                        "problem": problem,
                        "repaired": repair.repaired,
                        "detail": repair.detail,
                    },
                )
        logger.info(
            "reconcile_pass",
            checked=report.checked,
            diverged=report.diverged,
            repaired=report.repaired,
        )
        return report

    def _recent(self, limit: int) -> list[dict[str, Any]]:
        """Return recently finished runs, newest first."""
        return self.ledger.finished_runs(limit=limit, max_age_days=MAX_AGE_DAYS)

    async def _check(self, state: RunState, outcome: Outcome) -> verify_step.Consistency:
        """Run the consistency assertion again, reading only."""
        return await verify_step.assert_consistent(
            outcome=outcome,
            run_id=state.run_id,
            github=self.apps.github,
            linear=self.apps.linear,
            discord=self.apps.discord,
            pr_number=state.pr_number,
            linear_issue_id=state.linear_issue_id,
            discord_message_id=state.discord_reply_id,
            discord_channel_id=state.report.channel_id,
            expect_ready=state.pr_ready,
        )

    async def _repair(self, state: RunState, outcome: Outcome, app: str, problem: str) -> Repair:
        """Repair one divergence, or explain why it is left for a human.

        Only writes the run was already entitled to make are re-applied, through
        the same guard, carrying the same run marker. A divergence the run has no
        recorded conclusion for is reported rather than guessed at.
        """
        try:
            if app == "linear" and state.linear_issue_id:
                # The divergence is that the issue no longer carries this run's
                # marker, so the repair has to restore that, not merely add a
                # link beside it. A repair that leaves the check still failing is
                # not a repair.
                await self._rewrite(
                    state,
                    self.apps.linear.comment,
                    "linear.comment",
                    target=str(state.linear_issue_id),
                    issue_id=state.linear_issue_id,
                    body=(
                        f"Restoring the record for this run: {outcome.value}. "
                        f"{state.pr_url or state.report.url or ''}"
                    ),
                )
                if state.pr_url:
                    await self._rewrite(
                        state,
                        self.apps.linear.attach_url,
                        "linear.attach_url",
                        target=str(state.linear_issue_id),
                        issue_id=state.linear_issue_id,
                        url=state.pr_url,
                        title=f"Pull request #{state.pr_number}",
                    )
                return Repair(state.run_id, app, problem, repaired=True, detail="record re-applied")

            if app == "discord" and state.report.channel_id and state.linear_url:
                await self._rewrite(
                    state,
                    self.apps.discord.reply,
                    "discord.reply",
                    target=str(state.report.channel_id),
                    channel_id=state.report.channel_id,
                    reply_to_message_id=state.report.message_id,
                    content=(
                        f"Following up on this: {state.pr_url or state.linear_url} "
                        f"({outcome.value})."
                    ),
                )
                return Repair(state.run_id, app, problem, repaired=True, detail="reply re-sent")
        except Exception as error:  # noqa: BLE001 - a failed repair is reported, not raised
            return Repair(state.run_id, app, problem, repaired=False, detail=str(error))

        # GitHub divergence means a pull request is missing or in the wrong
        # state. Re-opening one would be a new decision, so it is not made here.
        return Repair(
            state.run_id, app, problem, repaired=False, detail="needs a human: no safe repair"
        )

    async def _rewrite(
        self,
        state: RunState,
        method: Any,  # noqa: ANN401
        operation: str,
        *,
        target: str,
        **payload: Any,  # noqa: ANN401
    ) -> None:
        """Re-apply one write, guarded and marked like the original."""
        intent = WriteIntent(
            run_id=state.run_id,
            app=operation.split(".", 1)[0],
            operation=operation,
            target=target,
            payload=payload,
        )
        result = await method(intent)
        adapter = getattr(self.apps, intent.app)
        verified = await adapter.readback(result)
        self.ledger.record_write(verified)
