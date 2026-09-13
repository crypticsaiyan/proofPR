"""The patch loop.

Section 4.6 of AGENTS.md. By the time this runs, a test exists that fails on the
base commit for the reason the reporter's input actually produced. The patch has
one job: make that test pass without breaking anything else.

Three attempts. Each sees the previous failure output, because a model that is
told "the suite failed" repeats itself, and one that is shown which assertion
failed usually does not. A patch is checked structurally before it is applied,
against the same allowlist that governs every write, so a diff that reaches
outside `src/**` is refused without ever touching the worktree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from proofpr.domain.errors import GuardBlockedError, PermanentAppError
from proofpr.observability.logging import get_logger
from proofpr.prompts import PATCH_SYSTEM, PATCH_USER, PATCH_WITHOUT_TEST_USER
from proofpr.steps import worktree as wt
from proofpr.steps.gate import suite_passed

logger = get_logger("proofpr.patch")

MAX_ATTEMPTS = 3

#: A fix in this class touches one file. A patch spanning several is either
#: wrong or outside the fix class, and either way a human should see it first.
MAX_FILES = 1


class PatchedFile(BaseModel):
    """One file, rewritten in full."""

    path: str = Field(description="Repository-relative path, for example src/validkit/dates.py")
    content: str = Field(description="The complete new contents of the file.")


class ProposedPatch(BaseModel):
    """The model's proposed fix."""

    files: list[PatchedFile] = Field(min_length=1, max_length=MAX_FILES)
    summary: str = Field(default="", max_length=300)
    rationale: str = Field(default="", max_length=800)


@dataclass
class PatchAttempt:
    """What happened on one attempt."""

    number: int
    accepted: bool
    detail: str
    test_passed: bool = False
    suite_passed: bool = False


@dataclass
class PatchResult:
    """The outcome of the loop."""

    patch: ProposedPatch | None
    attempts: list[PatchAttempt] = field(default_factory=list)
    originals: dict[str, str | None] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """Whether a patch made the new test pass with the suite still green."""
        return self.patch is not None and bool(self.attempts) and self.attempts[-1].accepted


def validate(patch: ProposedPatch, *, policy: Any, worktree_path: Path) -> str | None:  # noqa: ANN401
    """Check a proposed patch before applying it. None means accepted.

    The same `check_diff` the guard applies to a push is used here, so a patch
    that would be refused at publication time is refused now, before any sandbox
    time is spent on it.
    """
    if len(patch.files) > MAX_FILES:
        return f"a fix may touch {MAX_FILES} file, this one touches {len(patch.files)}"

    changes: dict[str, str] = {}
    for file in patch.files:
        raw = file.path.strip()
        # Traversal is rejected outright rather than normalised away. Stripping
        # leading dots would silently turn `../../etc/passwd` into a plausible
        # repository path, which is how a traversal check becomes a traversal.
        if ".." in Path(raw).parts or raw.startswith("/"):
            return f"{raw!r} is outside the repository"
        path = raw.removeprefix("./")
        target = (worktree_path / path).resolve()
        if not target.is_relative_to(worktree_path.resolve()):
            return f"{path!r} is outside the repository"
        changes[path] = "modified"

    # The allowlist runs before anything else, so a patch aimed at a forbidden
    # path is refused for that reason rather than for a coincidental one.
    try:
        policy.check_diff(changes)
    except GuardBlockedError as blocked:
        return f"{blocked.rule}: {blocked}"

    for file in patch.files:
        path = file.path.strip().removeprefix("./")
        if not (worktree_path / path).is_file():
            return f"{path!r} does not exist, and a fix in this class does not add files"
        try:
            ast.parse(file.content)
        except SyntaxError as error:
            return f"{path!r} does not parse: {error}"
    return None


def resolve_source(worktree_path: Path, path_hint: str) -> tuple[str, str]:
    """Return the checkout-relative path and contents of the file a frame names.

    A traceback path is normalised for matching (`src/era.py` becomes `era.py`),
    so reading it verbatim from the worktree finds nothing, and a model shown an
    empty file writes a new one from scratch, deleting everything it never saw.
    The file is located the way the reproduction step locates it, and the model
    is told its real path so the patch lands on the file that exists.
    """
    found = wt.find_source_file(worktree_path, path_hint) if path_hint else None
    if found is None:
        return path_hint, ""
    relative = found.relative_to(worktree_path).as_posix()
    return relative, found.read_text(encoding="utf-8")


async def run(
    *,
    model: Any,  # noqa: ANN401 - a ModelPort
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    policy: Any,  # noqa: ANN401 - a Policy
    worktree_path: Path,
    test_path: str,
    test_code: str,
    traceback: str,
    source_path: str,
    sanitized_block: str,
    record_usage: Any = None,  # noqa: ANN401
    timeout_seconds: int | None = None,
) -> PatchResult:
    """Write a patch that makes the failing test pass, leaving the suite green.

    The reproduction test is written into the worktree first and stays there for
    the whole loop, because the point of every attempt is to make that one test
    pass.
    """
    wt.write_file(worktree_path, test_path, test_code)
    source_path, source = resolve_source(worktree_path, source_path)
    result = PatchResult(patch=None)

    user = PATCH_USER.format(
        traceback=traceback[-3000:],
        test_path=test_path,
        test_code=test_code,
        source_path=source_path,
        source=source,
        block=sanitized_block,
    )

    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            proposal, usage = await model.complete_json(
                system=PATCH_SYSTEM,
                user=user,
                schema=ProposedPatch,
                tier="strong",
                max_tokens=4000,
            )
        except PermanentAppError as error:
            result.attempts.append(PatchAttempt(number, accepted=False, detail=str(error)))
            break
        if record_usage is not None:
            record_usage(usage)

        rejection = validate(proposal, policy=policy, worktree_path=worktree_path)
        if rejection is not None:
            logger.info("patch_rejected", attempt=number, detail=rejection)
            result.attempts.append(PatchAttempt(number, accepted=False, detail=rejection))
            user = _retry_prompt(user, proposal, rejection)
            continue

        originals = {file.path: wt.read_file(worktree_path, file.path) for file in proposal.files}
        for file in proposal.files:
            wt.write_file(worktree_path, file.path, file.content)

        test_run = await sandbox.run_pytest(
            worktree=str(worktree_path), target=test_path, timeout_seconds=timeout_seconds
        )
        if test_run["exit_code"] != 0:
            detail = _tail(test_run)
            logger.info("patch_test_failed", attempt=number)
            result.attempts.append(
                PatchAttempt(number, accepted=False, detail=detail, test_passed=False)
            )
            _revert(worktree_path, originals)
            user = _retry_prompt(user, proposal, f"the reproduction test still fails:\n{detail}")
            continue

        suite_run = await sandbox.run_pytest(
            worktree=str(worktree_path), target=".", timeout_seconds=timeout_seconds
        )
        if not suite_passed(suite_run["exit_code"]):
            detail = _tail(suite_run)
            logger.info("patch_suite_failed", attempt=number)
            result.attempts.append(
                PatchAttempt(
                    number, accepted=False, detail=detail, test_passed=True, suite_passed=False
                )
            )
            _revert(worktree_path, originals)
            user = _retry_prompt(
                user, proposal, f"the reproduction test passes but the suite broke:\n{detail}"
            )
            continue

        result.patch = proposal
        result.originals = originals
        result.attempts.append(
            PatchAttempt(
                number,
                accepted=True,
                detail="the reproduction test passes and the suite is green",
                test_passed=True,
                suite_passed=True,
            )
        )
        return result

    return result


async def run_without_test(
    *,
    model: Any,  # noqa: ANN401 - a ModelPort
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    policy: Any,  # noqa: ANN401 - a Policy
    worktree_path: Path,
    report_text: str,
    sanitized_block: str,
    source_path: str,
    exception: str | None,
    record_usage: Any = None,  # noqa: ANN401
    timeout_seconds: int | None = None,
) -> PatchResult:
    """Patch from the report alone, with no reproduction test to satisfy.

    This exists for the `no_gate` evaluation arm, and only for it. It is the
    shape of a patch written by an agent that was never asked to prove anything:
    the only check is that the project's own suite still passes, which a patch
    that does nothing also passes.

    It is deliberately in the same module as the real loop, so the difference
    between them is one function apart and cannot be argued about.
    """
    source_path, source = resolve_source(worktree_path, source_path)
    result = PatchResult(patch=None)
    user = PATCH_WITHOUT_TEST_USER.format(
        report=report_text[:3000],
        exception=exception or "an error",
        source_path=source_path,
        source=source,
        block=sanitized_block,
    )

    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            proposal, usage = await model.complete_json(
                system=PATCH_SYSTEM,
                user=user,
                schema=ProposedPatch,
                tier="strong",
                max_tokens=4000,
            )
        except PermanentAppError as error:
            result.attempts.append(PatchAttempt(number, accepted=False, detail=str(error)))
            break
        if record_usage is not None:
            record_usage(usage)

        rejection = validate(proposal, policy=policy, worktree_path=worktree_path)
        if rejection is not None:
            result.attempts.append(PatchAttempt(number, accepted=False, detail=rejection))
            user = _retry_prompt(user, proposal, rejection)
            continue

        originals = {file.path: wt.read_file(worktree_path, file.path) for file in proposal.files}
        for file in proposal.files:
            wt.write_file(worktree_path, file.path, file.content)

        suite = await sandbox.run_pytest(
            worktree=str(worktree_path), target=".", timeout_seconds=timeout_seconds
        )
        if not suite_passed(suite["exit_code"]):
            detail = _tail(suite)
            result.attempts.append(PatchAttempt(number, accepted=False, detail=detail))
            _revert(worktree_path, originals)
            user = _retry_prompt(user, proposal, f"the suite broke:\n{detail}")
            continue

        result.patch = proposal
        result.originals = originals
        result.attempts.append(
            PatchAttempt(
                number,
                accepted=True,
                detail="the suite is green",
                test_passed=False,
                suite_passed=True,
            )
        )
        return result

    return result


def _revert(worktree_path: Path, originals: dict[str, str | None]) -> None:
    """Put the worktree back as it was before a rejected attempt."""
    for path, original in originals.items():
        wt.revert_file(worktree_path, path, original)


def _tail(result: dict[str, Any], limit: int = 2000) -> str:
    """Return the useful end of a pytest run's output."""
    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".strip()
    return output[-limit:]


def _retry_prompt(user: str, proposal: ProposedPatch, problem: str) -> str:
    """Extend the prompt with the previous attempt and why it was refused."""
    changed = "\n\n".join(
        f"{file.path}:\n```python\n{file.content}\n```" for file in proposal.files
    )
    return (
        f"{user}\n\nYour previous attempt did not work.\n\n{changed}\n\n"
        f"Problem: {problem}\n\n"
        "Return a corrected patch. Change as little as possible."
    )
