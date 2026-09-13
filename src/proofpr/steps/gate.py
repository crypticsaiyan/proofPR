"""The reproduction gate.

Section 4.5 of AGENTS.md, and the reason this project exists. A test that merely
fails is worthless: it can fail because it does not import, because the suite was
already broken, or because it asserts something unrelated to the report. The gate
turns "there is a failing test" into "there is a test that fails for the reason
the reporter described, on an otherwise healthy checkout".

Nothing downstream runs unless every condition here holds, and the conditions are
checked in cost order so the cheapest disqualification happens first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofpr.domain.enums import Reason
from proofpr.steps import worktree as wt

#: pytest exit codes. 1 means tests failed, which is what a reproduction should
#: do; 2 to 5 mean pytest itself could not do its job.
PYTEST_TESTS_FAILED = 1
PYTEST_COLLECTION_ERROR = 2
PYTEST_NO_TESTS = 5

#: Failures that mean the test is broken rather than the code under test.
COLLECTION_ERRORS = frozenset(
    {
        "ImportError",
        "ModuleNotFoundError",
        "SyntaxError",
        "IndentationError",
        "NameError",
        "AttributeError",
        "FixtureLookupError",
    }
)

FAILURE_EXCEPTION = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Failed))", re.MULTILINE)

#: pytest rewrites a bare `assert` into an `E   assert ...` line with no
#: exception name anywhere in the output. Reading only named exceptions would
#: make every plain assertion look like "no exception", and the gate would reject
#: exactly the tests it is meant to accept.
ASSERT_LINE = re.compile(r"^E\s+(?:assert\b|\+\s+where\b)", re.MULTILINE)
COLLECT_ERROR_MARKER = re.compile(r"ERRORS?\b|errors? during collection|INTERNALERROR", re.I)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Whether a candidate test is a real reproduction."""

    passed: bool
    reason: Reason | None = None
    detail: str = ""
    observed_exception: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Allow the result to be used directly in a condition."""
        return self.passed


def suite_passed(exit_code: int) -> bool:
    """Whether a whole-suite pytest run counts as green.

    Exit 0 is green. Exit 5 means pytest collected nothing, which is a
    repository with no pytest suite rather than a failing one.
    """
    return exit_code in {0, PYTEST_NO_TESTS}


def failure_exception(output: str) -> str | None:
    """Return the exception a pytest failure reported, if any.

    A named exception wins. Failing that, a rewritten bare assertion is reported
    as ``AssertionError``, which is what it is.
    """
    matches = FAILURE_EXCEPTION.findall(output)
    if matches:
        return str(matches[-1]).split(".")[-1]
    return "AssertionError" if ASSERT_LINE.search(output) else None


async def run(
    *,
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    worktree_path: Path,
    test_path: str,
    test_code: str,
    expected_exception: str | None,
    timeout_seconds: int | None = None,
) -> GateResult:
    """Apply every gate condition to a candidate test.

    Args:
        sandbox: The sandbox to run in.
        worktree_path: A worktree at the base commit, with no patch applied.
        test_path: Where the candidate test will live.
        test_code: The candidate test.
        expected_exception: The exception stage 1 captured. The candidate must
            fail with this, or with an assertion.
        timeout_seconds: Overrides the sandbox timeout.

    Returns:
        The gate result, with one entry per condition so a failure says which.
    """
    checks: dict[str, bool] = {}

    # 1. The existing suite must be green before we add anything. A red suite
    #    makes every later signal meaningless, and it is not this agent's bug.
    #    A repository with no pytest suite at all (exit 5, nothing collected) is
    #    not red: nothing fails. Plenty of real projects test with scripts or not
    #    at all, and refusing them outright would make the tool useless on them.
    #    The reproduction test this run adds becomes that suite's first test.
    baseline = await sandbox.run_pytest(
        worktree=str(worktree_path), target=".", timeout_seconds=timeout_seconds
    )
    checks["suite_green_on_base"] = suite_passed(baseline["exit_code"])
    if not checks["suite_green_on_base"]:
        return GateResult(
            passed=False,
            reason=Reason.SUITE_RED_ON_BASE,
            detail="the target repository's own suite does not pass before any change",
            checks=checks,
        )

    original = wt.read_file(worktree_path, test_path)
    wt.write_file(worktree_path, test_path, test_code)
    try:
        result = await sandbox.run_pytest(
            worktree=str(worktree_path), target=test_path, timeout_seconds=timeout_seconds
        )
    finally:
        wt.revert_file(worktree_path, test_path, original)

    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
    exit_code = int(result["exit_code"])

    # 2. The test must collect. A test that cannot be imported has not been shown
    #    to fail for any reason at all.
    observed = failure_exception(output)
    collected = exit_code not in {PYTEST_COLLECTION_ERROR, PYTEST_NO_TESTS} and not (
        observed in COLLECTION_ERRORS and COLLECT_ERROR_MARKER.search(output)
    )
    checks["collects"] = collected
    if not collected:
        return GateResult(
            passed=False,
            reason=Reason.NO_FAILING_TEST,
            detail=f"the test did not collect (pytest exit {exit_code})",
            observed_exception=observed,
            checks=checks,
        )

    # 3. It must actually fail.
    checks["fails_on_base"] = exit_code == PYTEST_TESTS_FAILED
    if not checks["fails_on_base"]:
        return GateResult(
            passed=False,
            reason=Reason.NO_FAILING_TEST,
            detail=(
                "the test passes on the unmodified code, so it does not reproduce anything"
                if exit_code == 0
                else f"pytest exited {exit_code} rather than reporting a test failure"
            ),
            observed_exception=observed,
            checks=checks,
        )

    # 4. It must fail for the right reason. This is the condition that separates
    #    a reproduction from a coincidence.
    right_reason = _fails_for_the_right_reason(observed, expected_exception)
    checks["right_reason"] = right_reason
    if not right_reason:
        return GateResult(
            passed=False,
            reason=Reason.FAILS_FOR_WRONG_REASON,
            detail=(
                f"the test fails with {observed}, but the report reproduced {expected_exception}"
            ),
            observed_exception=observed,
            checks=checks,
        )

    return GateResult(passed=True, observed_exception=observed, checks=checks)


def _fails_for_the_right_reason(observed: str | None, expected: str | None) -> bool:
    """Return whether the failure matches what stage 1 captured.

    An assertion failure always counts: a test that asserts the corrected
    behaviour and fails is reproducing the bug just as surely as one that catches
    the exception. A different exception never counts, and neither does an
    exception nobody can name.
    """
    if observed is None:
        return False
    if observed in {"AssertionError", "Failed"}:
        return True
    if observed in COLLECTION_ERRORS:
        return False
    # Stage 1 reads the exception from a traceback, which qualifies it
    # (`json.decoder.JSONDecodeError`); pytest's summary line often does not.
    # The same exception under two spellings is the same reason.
    return expected is None or observed.split(".")[-1] == expected.split(".")[-1]
