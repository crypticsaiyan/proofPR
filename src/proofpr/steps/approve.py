"""The approval gate.

A maintainer decides whether this agent may push to their repository. The
decision is a port rather than a hard-coded prompt, because the three
configurations that matter are genuinely different: a maintainer clicking a
button, an evaluation run that must not block on a human, and a deployment that
has decided it trusts the proof.

Refusing is always safe. An approver that errors, times out, or cannot be
reached is treated as a refusal, never as consent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from proofpr.observability.logging import get_logger

logger = get_logger("proofpr.approve")


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What a maintainer is being asked to approve."""

    run_id: str
    summary: str
    proof_block: str
    patched_path: str
    test_path: str
    diff_lines: int
    report_url: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """The decision, and who made it."""

    approved: bool
    decided_by: str
    reason: str = ""


@runtime_checkable
class ApprovalPort(Protocol):
    """Asks a maintainer whether to publish."""

    async def request(self, request: ApprovalRequest) -> Approval:
        """Return the decision. Never raises; a failure is a refusal."""
        ...


class AutoApprover:
    """Approves without asking.

    Used in evaluation, where blocking on a human would make every number a
    measurement of human availability. Its decisions are labelled as automatic in
    the ledger so no reported result can be mistaken for a reviewed one.
    """

    async def request(self, request: ApprovalRequest) -> Approval:
        """Approve."""
        logger.info("approval_auto", run_id=request.run_id)
        return Approval(approved=True, decided_by="auto")


class DenyingApprover:
    """Refuses everything.

    The safe default for a deployment that has not configured an approver: an
    agent with no one to ask must not publish.
    """

    async def request(self, request: ApprovalRequest) -> Approval:
        """Refuse, whatever is being asked."""
        logger.info("approval_denied", run_id=request.run_id, reason="no approver configured")
        return Approval(
            approved=False,
            decided_by="policy",
            reason="no approver is configured, so nothing is published",
        )


async def request_approval(approver: Any, request: ApprovalRequest) -> Approval:  # noqa: ANN401
    """Ask an approver, treating any failure as a refusal.

    Args:
        approver: An :class:`ApprovalPort`, or None to refuse outright.
        request: What is being approved.

    Returns:
        The decision.
    """
    if approver is None:
        return Approval(approved=False, decided_by="policy", reason="no approver configured")
    try:
        decision: Approval = await approver.request(request)
    except Exception as error:  # noqa: BLE001 - an unreachable approver is a refusal
        logger.warning("approval_failed", run_id=request.run_id, error=str(error))
        return Approval(
            approved=False, decided_by="error", reason=f"the approver could not be reached: {error}"
        )
    return decision
