"""Scripted sandboxes for pipeline tests.

Driven by an explicit sequence of exit codes, because the order of sandbox calls
is part of the behaviour under test: baseline suite, candidate test, patch test,
patch suite, revert, flake, then one run per mutant.
"""

from __future__ import annotations

from typing import Any

CRASH_STDERR = """\
Traceback (most recent call last):
  File "proofpr_repro.py", line 12, in <module>
    result = parse_date("2024-13-01")
  File "src/validkit/dates.py", line 30, in parse_date
    if day > length:
TypeError: '>' not supported between instances of 'int' and 'NoneType'
"""


class ScriptedSandbox:
    """Returns scripted results, recording every call in order."""

    def __init__(
        self,
        *,
        script: dict[str, Any] | None = None,
        pytest_exit_codes: list[int] | None = None,
        default_exit_code: int = 0,
        failure_output: str = "E    TypeError: bad comparison",
    ) -> None:
        """Build the sandbox.

        Args:
            script: Result of the stage 1 script run. Crashes by default.
            pytest_exit_codes: Consumed in order by `run_pytest`.
            default_exit_code: Used once the list is exhausted.
            failure_output: stdout attached to any non-zero pytest result.
        """
        self.script = script or {
            "exit_code": 1,
            "stdout": "",
            "stderr": CRASH_STDERR,
            "timed_out": False,
        }
        self.pytest_exit_codes = list(pytest_exit_codes or [])
        self.default_exit_code = default_exit_code
        self.failure_output = failure_output
        self.calls: list[str] = []

    async def run(self, *, worktree: str, command: list[str], **kwargs: Any) -> dict[str, Any]:
        """Return the scripted stage 1 result."""
        self.calls.append(f"script:{command[-1]}")
        return self.script

    async def run_pytest(
        self, *, worktree: str, target: str = ".", **kwargs: Any
    ) -> dict[str, Any]:
        """Return the next scripted pytest result."""
        self.calls.append(f"pytest:{target}")
        code = self.pytest_exit_codes.pop(0) if self.pytest_exit_codes else self.default_exit_code
        return {
            "exit_code": code,
            "stdout": self.failure_output if code else "3 passed",
            "stderr": "",
            "timed_out": False,
        }


#: Baseline suite green, candidate test fails: the gate accepts.
GATE_PASSES = [0, 1]

#: Then the patch makes the test pass and the suite stays green, reverting
#: reintroduces the failure, three flake runs pass, and every mutant is killed.
PATCH_AND_PROOF_SUCCEED = [*GATE_PASSES, 0, 0, 1, 0, 0, 0]
