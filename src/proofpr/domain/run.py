"""Run state.

A snapshot of everything the pipeline has learned, written to the ledger after
every step. Resume rebuilds a run from the last snapshot, so the state must stay
serialisable and must never hold a client, a connection, or a credential.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from proofpr.domain.enums import Arm, Outcome, Reason, Step


class Report(BaseModel):
    """The incoming bug report, as received."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="Where it came from, for example discord or fixture.")
    text: str = Field(description="Raw reporter text. Always sanitized before a prompt.")
    author: str = ""
    channel_id: int | None = None
    message_id: int | None = None
    url: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExistsFinding(BaseModel):
    """What the exists check found, if anything."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(default="none", description="none, duplicate, already_fixed, fix_in_flight.")
    app: str | None = None
    reference: str | None = None
    url: str | None = None
    score: float = 0.0
    evidence: str = ""

    @property
    def found(self) -> bool:
        """Whether the work already exists somewhere."""
        return self.kind != "none"


class RunState(BaseModel):
    """Everything known about a run so far."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    arm: Arm = Arm.PROOFPR
    report: Report
    step: Step = Step.SANITIZE

    # Sanitize
    injection_flags: list[str] = Field(default_factory=list)
    sanitized_text: str = ""

    # Pre-check
    intent: str | None = None
    intent_confidence: float = 0.0
    intent_decided_by: str | None = None
    has_error_output: bool = False

    # Fingerprint and localization
    exception: str | None = None
    path: str | None = None
    function: str | None = None
    fingerprint_digest: str | None = None
    reported_version: str | None = None

    # Reproduction
    repro_outcome: str | None = None
    repro_origin: str | None = None
    repro_code: str | None = None
    repro_traceback: str = ""
    repro_exception: str | None = None
    test_path: str | None = None
    test_code: str | None = None
    test_attempts: int = 0
    gate_passed: bool = False
    gate_checks: dict[str, bool] = Field(default_factory=dict)
    clarify_question: str | None = None
    clarify_answer: str | None = None

    # Patch and proof
    patch_attempts: int = 0
    patch_summary: str | None = None
    patch_rationale: str | None = None
    patch_code: str | None = None
    patched_path: str | None = None
    patch_diff_lines: int = 0
    proof_complete: bool = False
    proof_block: str | None = None
    mutants_killed: int = 0
    mutants_total: int = 0

    # Publication
    approved_by: str | None = None
    branch: str | None = None
    pr_number: str | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    ci_outcome: str | None = None
    ci_summary: str = ""
    pr_ready: bool = False
    consistent: bool | None = None
    consistency_problems: list[str] = Field(default_factory=list)

    # Exists and scope
    exists: ExistsFinding = Field(default_factory=ExistsFinding)
    in_scope: bool = False
    fix_class: str | None = None
    scope_detail: str = ""

    # Writes made
    linear_issue_id: str | None = None
    linear_identifier: str | None = None
    linear_url: str | None = None
    discord_reply_id: str | None = None

    # Terminal
    outcome: Outcome | None = None
    reason: Reason | None = None
    receipt: str | None = None

    def advanced(self, step: Step, **fields: Any) -> Self:  # noqa: ANN401 - field values vary
        """Return a copy at a new step with the given fields updated."""
        return self.model_copy(update={"step": step, **fields})

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for the ledger."""
        return self.model_dump(mode="json")
