"""The worth-it check: rules only, no model.

Section 4.3 of AGENTS.md. "Is this worth working on" is the most subjective
question in the pipeline, which is exactly why no model is asked it. Four
deterministic conditions decide whether the agent may write code. Failing any of
them still files an issue for a human; it only stops the agent from patching.

The measurable consequence is a false-reject rate: in-scope reports wrongly
rejected, target zero, reported in the evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from proofpr.domain.enums import Reason
from proofpr.triage import version_range
from proofpr.triage.fingerprint import Fingerprint

#: Fix classes this agent is permitted to attempt. Anything else is a human's
#: job, however confident a model might be about it.
DEFAULT_FIX_CLASSES = ("input_validation", "bad_type_handling")

#: Exception types that characterise the permitted fix classes. A crash outside
#: this set may still be a real bug; it is simply not one this agent patches.
FIX_CLASS_EXCEPTIONS: dict[str, frozenset[str]] = {
    # JSONDecodeError and UnicodeDecodeError are ValueError subclasses raised on
    # malformed input, and they are how that bug usually surfaces in a traceback.
    "input_validation": frozenset(
        {
            "ValueError",
            "KeyError",
            "IndexError",
            "AssertionError",
            "JSONDecodeError",
            "UnicodeDecodeError",
        }
    ),
    "bad_type_handling": frozenset({"TypeError", "AttributeError"}),
}


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    """Whether the agent may write code for this report, and why not."""

    in_scope: bool
    reason: Reason | None = None
    detail: str = ""
    fix_class: str | None = None

    def __bool__(self) -> bool:
        """Allow the decision to be used directly in a condition."""
        return self.in_scope


def classify_fix_class(
    exception: str | None, allowed: tuple[str, ...] = DEFAULT_FIX_CLASSES
) -> str | None:
    """Return the fix class an exception belongs to, if it belongs to a permitted one."""
    if exception is None:
        return None
    for fix_class in allowed:
        if exception in FIX_CLASS_EXCEPTIONS.get(fix_class, frozenset()):
            return fix_class
    return None


def decide(
    fingerprint: Fingerprint,
    *,
    reported_version: str | None,
    supported: str,
    patch_paths: tuple[str, ...],
    fix_classes: tuple[str, ...] = DEFAULT_FIX_CLASSES,
) -> ScopeDecision:
    """Apply the four worth-it conditions in order.

    Args:
        fingerprint: The report's fingerprint.
        reported_version: The version the reporter named, or None when they did
            not name one.
        supported: The supported version specifier, for example ``>=0.3.0``.
        patch_paths: Glob patterns the agent may patch.
        fix_classes: Permitted fix classes.

    Returns:
        The decision, carrying a reason code when the report is out of scope.
    """
    if not fingerprint.is_structural:
        return ScopeDecision(
            in_scope=False,
            reason=Reason.INSUFFICIENT_REPORT,
            detail="no exception type and no stack frame, so there is nothing to localize",
        )

    # An empty specifier means the project does not publish versions (an
    # application, or a library that is only ever run from main). There is no
    # range a report could fall outside of, so no version is required.
    if supported.strip():
        if reported_version is None:
            return ScopeDecision(
                in_scope=False,
                reason=Reason.UNSUPPORTED_VERSION,
                detail="the report does not say which version it is running",
            )

        parsed = version_range.parse(reported_version)
        if parsed is None:
            return ScopeDecision(
                in_scope=False,
                reason=Reason.UNSUPPORTED_VERSION,
                detail=f"cannot parse the reported version {reported_version!r}",
            )
        if not version_range.satisfies(parsed, supported):
            return ScopeDecision(
                in_scope=False,
                reason=Reason.UNSUPPORTED_VERSION,
                detail=f"version {parsed} is outside the supported range {supported}",
            )

    fix_class = classify_fix_class(fingerprint.exception, fix_classes)
    if fix_class is None:
        return ScopeDecision(
            in_scope=False,
            reason=Reason.OUT_OF_FIX_CLASS,
            detail=(
                f"{fingerprint.exception} is outside the fix classes this agent attempts "
                f"({', '.join(fix_classes)})"
            ),
        )

    if fingerprint.path is None or not any(
        fnmatch(fingerprint.path, pattern) or fnmatch(f"src/{fingerprint.path}", pattern)
        for pattern in patch_paths
    ):
        return ScopeDecision(
            in_scope=False,
            reason=Reason.OUT_OF_ALLOWED_PATHS,
            detail=(
                f"the failing frame is in {fingerprint.path or 'an unknown file'}, "
                f"outside {', '.join(patch_paths)}"
            ),
        )

    return ScopeDecision(in_scope=True, fix_class=fix_class)
