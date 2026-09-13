"""Intent classification: deterministic rules first, one narrow model call second.

Section 4.1 of AGENTS.md. The asymmetry is the point. Stopping a real bug report
costs a user their bug; continuing on a greeting costs one cheap search. So the
rules only ever stop a message they are certain about, the model is asked a
single narrow question, and low confidence continues rather than stops.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, Field

#: Below this, the model's opinion is not acted on and triage continues.
CONFIDENCE_THRESHOLD = 0.6

#: Messages this short carry no report, whatever they say.
MIN_REPORT_LENGTH = 15

#: Greetings and acknowledgements, matched whole so "thanks, but it crashes" is
#: not swallowed.
CHATTER_PATTERN = re.compile(
    r"^\s*(?:hi|hey|hello|yo|thanks?|thank you|ty|ok|okay|cool|nice|lol|\+1|same|"
    r"good (?:morning|afternoon|evening)|gm|gn)[\s!.,?:)+1]*$",
    re.IGNORECASE,
)

#: Signals that a message contains real evidence, which no rule may stop.
EVIDENCE_PATTERN = re.compile(
    r"traceback \(most recent call last\)|\bFile \"|"
    r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b|^\s{2,}at |\bstack ?trace\b",
    re.IGNORECASE | re.MULTILINE,
)

REPRODUCTION_PATTERN = re.compile(
    r"\bsteps? to (?:reproduce|repro)\b|\brepro(?:duction)?\b|\bto reproduce\b|"
    r"\bwhen i (?:call|run|use|pass)\b|\bif you (?:call|run|pass)\b|```",
    re.IGNORECASE,
)


class Intent(StrEnum):
    """What a message is."""

    BUG = "bug"
    QUESTION = "question"
    FEATURE = "feature"
    CHATTER = "chatter"


class IntentVerdict(BaseModel):
    """The model's answer to the single intent question."""

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    has_reproduction_steps: bool = False
    has_error_output: bool = False
    rationale: str = Field(default="", max_length=300)


@dataclass(frozen=True, slots=True)
class PreCheck:
    """The pre-check result."""

    intent: Intent
    confidence: float
    decided_by: str
    has_error_output: bool = False
    has_reproduction_steps: bool = False
    rationale: str = ""

    @property
    def should_continue(self) -> bool:
        """Whether triage proceeds.

        Anything other than a confident non-bug continues. A low-confidence
        question is triaged like a bug report, because the search that follows is
        cheap and being wrong in the other direction is not.
        """
        return self.intent is Intent.BUG or self.confidence < CONFIDENCE_THRESHOLD


def rule_check(text: str) -> PreCheck | None:
    """Apply the deterministic rules, returning None when they do not decide.

    The rules are allowed to stop a message only when they are certain, and are
    allowed to force a bug classification whenever the message carries evidence.
    """
    stripped = text.strip()

    if EVIDENCE_PATTERN.search(stripped):
        # A traceback settles it. No model is asked, and no rule below may
        # override it: a message carrying a stack trace is a bug report even if
        # it also says hello.
        return PreCheck(
            intent=Intent.BUG,
            confidence=1.0,
            decided_by="rule:evidence",
            has_error_output=True,
            has_reproduction_steps=bool(REPRODUCTION_PATTERN.search(stripped)),
            rationale="the message contains error output",
        )

    if CHATTER_PATTERN.match(stripped):
        return PreCheck(
            intent=Intent.CHATTER,
            confidence=1.0,
            decided_by="rule:greeting",
            rationale="the message is a greeting or acknowledgement",
        )

    if len(stripped) < MIN_REPORT_LENGTH:
        return PreCheck(
            intent=Intent.CHATTER,
            confidence=1.0,
            decided_by="rule:too_short",
            rationale=f"the message is {len(stripped)} characters, too short to be a report",
        )

    return None


def from_verdict(verdict: IntentVerdict, text: str) -> PreCheck:
    """Turn a model verdict into a pre-check result."""
    return PreCheck(
        intent=verdict.intent,
        confidence=verdict.confidence,
        decided_by="model",
        has_error_output=verdict.has_error_output,
        has_reproduction_steps=(
            verdict.has_reproduction_steps or bool(REPRODUCTION_PATTERN.search(text))
        ),
        rationale=verdict.rationale,
    )
