"""Waiting for CI, and reading the answer from the API.

The proof block claims CI was green. That claim is only worth something if it
comes from the checks API rather than from a local run, so this is the one thing
in the pipeline that genuinely waits.

Absence of checks is never success. A repository with no CI configured leaves the
pull request in draft, which is the honest outcome: nobody has verified anything
beyond what the sandbox did.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from proofpr.observability.logging import get_logger

logger = get_logger("proofpr.ci")

DEFAULT_POLL_SECONDS = 15
DEFAULT_TIMEOUT_MINUTES = 20

#: Checks can take a moment to appear after a push. Concluding "no CI" during
#: that window would leave every fast run in draft.
INITIAL_GRACE_SECONDS = 30


class CiOutcome(StrEnum):
    """What CI said."""

    SUCCESS = "success"
    FAILURE = "failure"
    TIMED_OUT = "timed_out"
    NO_CHECKS = "no_checks"


@dataclass(frozen=True, slots=True)
class CiResult:
    """The conclusion, and how long it took to get it."""

    outcome: CiOutcome
    waited_seconds: int
    checks: list[dict[str, Any]]

    @property
    def succeeded(self) -> bool:
        """Whether the pull request may leave draft."""
        return self.outcome is CiOutcome.SUCCESS

    @property
    def summary(self) -> str:
        """One line for the reply and the issue."""
        names = ", ".join(str(check.get("name", "check")) for check in self.checks[:3])
        if self.outcome is CiOutcome.SUCCESS:
            return f"CI passed ({names}) after {self.waited_seconds}s"
        if self.outcome is CiOutcome.FAILURE:
            failed = [
                str(check.get("name"))
                for check in self.checks
                if check.get("conclusion") not in {"success", "neutral", "skipped"}
            ]
            return f"CI failed: {', '.join(failed) or names}"
        if self.outcome is CiOutcome.NO_CHECKS:
            return "no CI checks ran, so nothing independent verified this"
        return f"CI did not finish within {self.waited_seconds}s"


async def wait(
    *,
    github: Any,  # noqa: ANN401 - a GitHubPort
    ref: str,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    sleep: Any = asyncio.sleep,  # noqa: ANN401 - injected so tests do not wait
) -> CiResult:
    """Poll the checks API until CI concludes, fails, or the timeout expires.

    Args:
        github: The GitHub adapter.
        ref: The head commit to check.
        poll_seconds: Delay between polls.
        timeout_minutes: Give up after this long.
        sleep: Injected sleep, so tests run instantly.

    Returns:
        The conclusion, the elapsed time, and the checks it was read from.
    """
    deadline = timeout_minutes * 60
    started = time.monotonic()
    checks: list[dict[str, Any]] = []

    while True:
        checks = await github.get_check_runs(ref)
        waited = int(time.monotonic() - started)

        if checks:
            if all(check.get("status") == "completed" for check in checks):
                passed = all(
                    check.get("conclusion") in {"success", "neutral", "skipped"} for check in checks
                )
                outcome = CiOutcome.SUCCESS if passed else CiOutcome.FAILURE
                logger.info("ci_concluded", outcome=outcome.value, waited=waited, ref=ref)
                return CiResult(outcome=outcome, waited_seconds=waited, checks=checks)
        elif waited >= INITIAL_GRACE_SECONDS:
            logger.info("ci_no_checks", ref=ref, waited=waited)
            return CiResult(outcome=CiOutcome.NO_CHECKS, waited_seconds=waited, checks=[])

        if waited >= deadline:
            logger.info("ci_timed_out", ref=ref, waited=waited)
            return CiResult(outcome=CiOutcome.TIMED_OUT, waited_seconds=waited, checks=checks)

        await sleep(poll_seconds)
