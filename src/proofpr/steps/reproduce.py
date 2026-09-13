"""Orchestration of the two reproduction stages and the clarifying question.

Kept out of `pipeline.py` because reproduction is the one part of the run with
real branching: it can confirm, it can ask a question and try again, and it can
confirm a crash while still failing to express it as a test. The pipeline stays a
straight line; the branching lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofpr.domain.errors import PermanentAppError
from proofpr.observability.logging import get_logger
from proofpr.prompts import (
    CLARIFY_SYSTEM,
    CLARIFY_USER,
    TEST_SYNTH_SYSTEM,
    TEST_SYNTH_USER,
)
from proofpr.steps import gate as gate_step
from proofpr.steps import repro_raw
from proofpr.steps import worktree as wt
from proofpr.steps.test_synth import MAX_ATTEMPTS, Rejection, SynthesizedTest, validate

logger = get_logger("proofpr.reproduce")

#: How much of a source file is worth showing the model. A function that needs
#: more context than this to fix is outside the fix class anyway.
MAX_SOURCE_CHARS = 8000


@dataclass
class SynthResult:
    """The outcome of stage 2, including everything that was rejected."""

    test: SynthesizedTest | None
    attempts: int
    rejections: list[Rejection]
    gate: gate_step.GateResult | None = None

    @property
    def succeeded(self) -> bool:
        """Whether a test survived both validation and the gate."""
        return self.test is not None and self.gate is not None and self.gate.passed


async def synthesize(
    *,
    model: Any,  # noqa: ANN401 - a ModelPort
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    worktree_path: Path,
    raw: repro_raw.RawRepro,
    sanitized_block: str,
    package: str,
    record_usage: Any = None,  # noqa: ANN401
    timeout_seconds: int | None = None,
) -> SynthResult:
    """Turn a captured traceback into a test that passes the gate.

    Each attempt sees why the previous one was refused, whether it was refused by
    the structural rules or by the gate. Three attempts, then the run files what
    it has: a confirmed crash with a real traceback is worth a human's time even
    without a test.
    """
    source_path = raw.path or ""
    source = _read_source(worktree_path, source_path)
    rejections: list[Rejection] = []
    last_gate: gate_step.GateResult | None = None

    user = TEST_SYNTH_USER.format(
        traceback=raw.traceback[-4000:],
        repro_code=raw.code,
        source_path=source_path or "unknown",
        source=source,
        block=sanitized_block,
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            candidate, usage = await model.complete_json(
                system=TEST_SYNTH_SYSTEM,
                user=user,
                schema=SynthesizedTest,
                tier="strong",
                max_tokens=2000,
            )
        except PermanentAppError as error:
            rejections.append(Rejection("model", str(error)))
            break
        if record_usage is not None:
            record_usage(usage)

        rejection = validate(candidate, package=package)
        if rejection is not None:
            logger.info("test_rejected", attempt=attempt, rule=rejection.rule)
            rejections.append(rejection)
            user = _retry_prompt(user, candidate, f"{rejection.rule}: {rejection.detail}")
            continue

        last_gate = await gate_step.run(
            sandbox=sandbox,
            worktree_path=worktree_path,
            test_path=candidate.path,
            test_code=candidate.code,
            expected_exception=raw.exception,
            timeout_seconds=timeout_seconds,
        )
        if last_gate.passed:
            return SynthResult(
                test=candidate, attempts=attempt, rejections=rejections, gate=last_gate
            )

        logger.info("gate_failed", attempt=attempt, reason=last_gate.reason)
        rejections.append(Rejection(str(last_gate.reason), last_gate.detail))
        if last_gate.reason is not None and last_gate.reason.value == "suite_red_on_base":
            # Not the test's fault, and no retry will change it.
            break
        user = _retry_prompt(user, candidate, last_gate.detail)

    return SynthResult(test=None, attempts=MAX_ATTEMPTS, rejections=rejections, gate=last_gate)


async def ask_clarifying_question(
    *,
    model: Any,  # noqa: ANN401 - a ModelPort
    sanitized_block: str,
    attempted: str,
    record_usage: Any = None,  # noqa: ANN401
) -> str:
    """Compose the single question to send back to the reporter."""
    from pydantic import BaseModel, Field

    class Question(BaseModel):
        question: str = Field(max_length=300)
        asking_for: str = "input"

    answer, usage = await model.complete_json(
        system=CLARIFY_SYSTEM,
        user=CLARIFY_USER.format(block=sanitized_block, attempted=attempted),
        schema=Question,
        tier="cheap",
        max_tokens=200,
    )
    if record_usage is not None:
        record_usage(usage)
    question: str = answer.question
    return question


def fallback_question(raw: repro_raw.RawRepro) -> str:
    """A question that needs no model, used when none is configured.

    Worth having: the clarification path is the difference between a vague report
    becoming a fix and becoming a shrug, and it should not depend on a model
    being reachable.
    """
    if raw.outcome is repro_raw.ReproOutcome.MISSING_INPUT:
        return (
            "I could not find a runnable example in the report. "
            "What exact call or input triggers this?"
        )
    if raw.outcome is repro_raw.ReproOutcome.NO_CRASH:
        return (
            f"I ran `{raw.code}` and it did not raise. "
            "What input are you passing, and which version are you on?"
        )
    if raw.outcome is repro_raw.ReproOutcome.ENVIRONMENT:
        return (
            f"Running the reported input failed with {raw.exception} before reaching the library, "
            "which usually means an environment difference. How is it installed?"
        )
    return "What exact input triggers this, and which version are you running?"


def _read_source(worktree_path: Path, path_hint: str) -> str:
    """Return the source of the file a traceback named, truncated."""
    if not path_hint:
        return "(not identified)"
    found = wt.find_source_file(worktree_path, path_hint)
    if found is None:
        return "(not found in the repository)"
    text = found.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= MAX_SOURCE_CHARS else f"{text[:MAX_SOURCE_CHARS]}\n# ...truncated"


def _retry_prompt(user: str, candidate: SynthesizedTest, problem: str) -> str:
    """Extend the prompt with the previous attempt and why it was refused."""
    return (
        f"{user}\n\nYour previous attempt was refused.\n\n"
        f"```python\n{candidate.code}\n```\n\n"
        f"Reason: {problem}\n\n"
        "Return a corrected file. Do not repeat the same mistake."
    )
