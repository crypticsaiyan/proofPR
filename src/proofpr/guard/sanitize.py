"""The untrusted content boundary.

Every piece of application-sourced text passes through here before it reaches a
model. Two things happen, and only two:

1. The text is wrapped in a delimited block that says plainly it is data.
2. Instruction-shaped content is flagged, and the flags are recorded.

The text is **not** rewritten. Stripping suspicious phrases would corrupt genuine
bug reports, which routinely contain words like "ignore" and "override", and it
would hide the attack from the evaluation. Injection is neutralised by
architecture, not by filtering: the model has no tools, so instructions inside a
report have nothing to act on. See ADR 0001.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from proofpr.domain.models import UntrustedText

#: Marker pair around untrusted content. Randomised per run by the caller when a
#: report could plausibly contain the literal marker text.
OPEN_MARKER = "<untrusted source={source!r}>"
CLOSE_MARKER = "</untrusted>"

#: Rule-based injection signals. These feed a recorded flag and a metric. They
#: never gate the pipeline on their own, because a false positive would discard a
#: real bug report, and a false negative costs nothing given the model has no
#: tools.
INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|above|prior|earlier|all)\b", re.I
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\byou are now\b|\bact as\b|\bnew (?:system )?(?:prompt|instructions?)\b", re.I
        ),
    ),
    (
        "privilege_request",
        re.compile(
            # Wide enough for "add GitHub user `someone-long` as a collaborator".
            r"\badd\b.{0,60}\b(collaborator|admin|maintainer|owner)s?\b"
            r"|\bgrant\b.{0,40}\b(access|admin|write)\b",
            re.I,
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(print|reveal|show|output|send|post)\b.{0,40}"
            r"\b(env|environment|secret|token|api[_ ]?key|credential)\b",
            re.I,
        ),
    ),
    (
        "destructive_request",
        re.compile(
            r"\b(close|delete|remove|revert|force[- ]push|merge)\b.{0,30}"
            r"\b(issue|pr|pull request|branch|repo|main)\b",
            re.I,
        ),
    ),
    (
        "protected_branch_push",
        re.compile(
            r"\b(push|commit|merge)\b.{0,40}\b(straight|directly|right)?\s*(to|into|onto)\s+"
            r"(the\s+)?(main|master|default branch|production)\b",
            re.I,
        ),
    ),
    (
        "approval_bypass",
        re.compile(
            r"\b(already|pre-?)\s*approved\b|\bskip\b.{0,20}\b(review|approval|ci|tests?)\b"
            r"|\bwithout\b.{0,20}\b(review|approval)\b|\bdon'?t\b.{0,20}\bwait\b.{0,20}\breview\b",
            re.I,
        ),
    ),
    (
        "workflow_tampering",
        re.compile(r"\.github/workflows|\bci\.ya?ml\b|\bbranch protection\b", re.I),
    ),
    ("marker_forgery", re.compile(r"</?untrusted|proofpr-run:", re.I)),
    ("hidden_text", re.compile(r"<!--.{0,200}-->", re.S)),
)

#: Characters that render as nothing but change how text is read. They are
#: removed, not flagged, because they carry no legitimate meaning in a bug report.
INVISIBLE_CHARS = re.compile(r"[​-‏‪-‮⁦-⁩﻿]")


@dataclass(frozen=True, slots=True)
class Sanitized:
    """Untrusted text prepared for a prompt, with its injection flags."""

    source: str
    text: str
    flags: tuple[str, ...] = field(default_factory=tuple)
    removed_invisible: int = 0

    @property
    def flagged(self) -> bool:
        """Whether any injection rule matched."""
        return bool(self.flags)

    def block(self) -> str:
        """Render the delimited block that a prompt may embed.

        The block states its own provenance so the surrounding prompt does not
        have to be trusted to describe it correctly.
        """
        opener = OPEN_MARKER.format(source=self.source)
        return f"{opener}\n{self.text}\n{CLOSE_MARKER}"


def sanitize(text: UntrustedText | str, *, source: str | None = None) -> Sanitized:
    """Prepare application text for a prompt and flag instruction-shaped content.

    Args:
        text: The untrusted text, either wrapped or raw.
        source: Provenance label, required when ``text`` is a raw string.

    Returns:
        The sanitized block and its flags.

    Raises:
        ValueError: A raw string was passed without a source.
    """
    if isinstance(text, UntrustedText):
        raw, origin = text.value, text.source
    else:
        if source is None:
            raise ValueError("source is required when sanitizing a raw string")
        raw, origin = text, source

    normalized = unicodedata.normalize("NFKC", raw)
    without_invisible = INVISIBLE_CHARS.sub("", normalized)
    removed = len(normalized) - len(without_invisible)

    # Neutralise forged delimiters so injected text cannot close the block and
    # appear to be prompt. The content is preserved, only the delimiter is broken.
    escaped = without_invisible.replace("</untrusted", "<​/untrusted").replace(
        "<untrusted", "<​untrusted"
    )

    flags = tuple(name for name, pattern in INJECTION_RULES if pattern.search(without_invisible))
    return Sanitized(source=origin, text=escaped, flags=flags, removed_invisible=removed)
