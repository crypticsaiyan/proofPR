"""The gate: a failing test is not a reproduction until it fails for the right reason."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Reason
from proofpr.steps import gate as gate_step
from proofpr.steps.test_synth import SynthesizedTest, validate

GOOD_TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
"""


class ScriptedSandbox:
    """A sandbox returning scripted pytest results in order."""

    def __init__(self, *results: dict[str, Any]) -> None:
        """Build the stub with one result per expected invocation."""
        self.results = list(results)
        self.targets: list[str] = []

    async def run_pytest(
        self, *, worktree: str, target: str = ".", **kwargs: Any
    ) -> dict[str, Any]:
        """Return the next scripted result."""
        self.targets.append(target)
        return self.results.pop(0)


def result(exit_code: int, stdout: str = "") -> dict[str, Any]:
    """Build a sandbox result."""
    return {"exit_code": exit_code, "stdout": stdout, "stderr": "", "timed_out": False}


async def run_gate(sandbox: Any, tmp_path: Path, *, expected: str | None = "TypeError") -> Any:
    """Run the gate against a scripted sandbox."""
    (tmp_path / "tests").mkdir(exist_ok=True)
    return await gate_step.run(
        sandbox=sandbox,
        worktree_path=tmp_path,
        test_path="tests/test_repro.py",
        test_code=GOOD_TEST,
        expected_exception=expected,
    )


class TestGate:
    """Every condition, in cost order."""

    async def test_a_red_suite_on_base_stops_everything(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(result(1, "2 failed"))

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is False
        assert outcome.reason is Reason.SUITE_RED_ON_BASE
        # No point running the candidate against a checkout that is already broken.
        assert sandbox.targets == ["."]

    async def test_a_test_that_fails_with_the_captured_exception_passes(
        self, tmp_path: Path
    ) -> None:
        sandbox = ScriptedSandbox(
            result(0, "3 passed"),
            result(1, "E       TypeError: '>' not supported\n1 failed"),
        )

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is True
        assert outcome.observed_exception == "TypeError"
        assert outcome.checks == {
            "suite_green_on_base": True,
            "collects": True,
            "fails_on_base": True,
            "right_reason": True,
        }

    async def test_an_assertion_failure_always_counts(self, tmp_path: Path) -> None:
        # A test asserting the corrected behaviour reproduces the bug just as
        # surely as one catching the exception.
        sandbox = ScriptedSandbox(
            result(0, "3 passed"), result(1, "E       AssertionError: assert 13 <= 12\n1 failed")
        )

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is True

    async def test_a_test_that_passes_reproduces_nothing(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(result(0, "3 passed"), result(0, "1 passed"))

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is False
        assert outcome.reason is Reason.NO_FAILING_TEST
        assert "passes on the unmodified code" in outcome.detail

    async def test_a_test_that_does_not_import_is_not_a_reproduction(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(
            result(0, "3 passed"),
            result(2, "ERRORS\nE   ImportError: cannot import name 'parse_dates'"),
        )

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is False
        assert outcome.reason is Reason.NO_FAILING_TEST
        assert outcome.checks["collects"] is False

    async def test_failing_with_a_different_exception_is_refused(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(
            result(0, "3 passed"), result(1, "E       KeyError: 13\n1 failed")
        )

        outcome = await run_gate(sandbox, tmp_path, expected="TypeError")

        assert outcome.passed is False
        assert outcome.reason is Reason.FAILS_FOR_WRONG_REASON
        assert outcome.observed_exception == "KeyError"

    async def test_collecting_nothing_is_refused(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(result(0, "3 passed"), result(5, "no tests ran"))

        outcome = await run_gate(sandbox, tmp_path)

        assert outcome.passed is False
        assert outcome.checks["collects"] is False

    async def test_the_candidate_is_removed_again_afterwards(self, tmp_path: Path) -> None:
        sandbox = ScriptedSandbox(result(0, "3 passed"), result(0, "1 passed"))

        await run_gate(sandbox, tmp_path)

        # The worktree must be left as it was found, or the next measurement is
        # taken against a checkout this run modified.
        assert not (tmp_path / "tests" / "test_repro.py").exists()


class TestSynthesisValidation:
    """A proposed test is checked before it is ever run."""

    def make(self, code: str = GOOD_TEST, path: str = "tests/test_repro.py") -> SynthesizedTest:
        """Build a candidate."""
        return SynthesizedTest(path=path, code=code)

    def test_a_good_test_is_accepted(self) -> None:
        assert validate(self.make(), package="validkit") is None

    @pytest.mark.parametrize(
        "path",
        ["src/test_x.py", "tests/sub/test_x.py", "tests/helpers.py", "test_x.py"],
    )
    def test_paths_outside_the_test_directory_are_refused(self, path: str) -> None:
        rejection = validate(self.make(path=path), package="validkit")

        assert rejection is not None
        assert rejection.rule == "path"

    def test_a_file_that_does_not_parse_is_refused(self) -> None:
        rejection = validate(self.make("def test_x(:\n    pass"), package="validkit")

        assert rejection is not None
        assert rejection.rule == "syntax"

    def test_more_than_one_test_is_refused(self) -> None:
        code = "def test_a():\n    assert False\n\ndef test_b():\n    assert False\n"

        rejection = validate(self.make(code), package="validkit")

        assert rejection is not None
        assert rejection.rule == "one_test"

    @pytest.mark.parametrize("module", ["requests", "os", "subprocess", "unittest.mock", "time"])
    def test_imports_beyond_pytest_and_the_package_are_refused(self, module: str) -> None:
        code = f"import {module}\n\ndef test_x():\n    assert False\n"

        rejection = validate(self.make(code), package="validkit")

        assert rejection is not None
        assert rejection.rule == "import"

    def test_a_test_that_asserts_nothing_is_refused(self) -> None:
        code = "from validkit import parse_date\n\ndef test_x():\n    parse_date('2024-13-01')\n"

        rejection = validate(self.make(code), package="validkit")

        assert rejection is not None
        assert rejection.rule == "no_assertion"

    def test_pytest_raises_counts_as_asserting(self) -> None:
        assert validate(self.make(), package="validkit") is None
