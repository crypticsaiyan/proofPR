"""Waiting for CI: read the answer, never assume it."""

from __future__ import annotations

from typing import Any

import pytest

from proofpr.steps.ci_wait import CiOutcome, wait


class FakeGitHub:
    """Returns scripted check-run responses in order."""

    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        """Build the stub."""
        self.responses = list(responses)
        self.polls = 0

    async def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        """Return the next scripted response, repeating the last one."""
        self.polls += 1
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def check(status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
    """Build one check run."""
    return {"name": "ci", "status": status, "conclusion": conclusion}


async def no_sleep(seconds: float) -> None:
    """Sleep instantly, so the test suite does not wait for CI."""


async def run(github: Any, **kwargs: Any) -> Any:
    """Wait for CI with an instant clock."""
    return await wait(github=github, ref="abc", sleep=no_sleep, **kwargs)


async def test_a_green_check_succeeds() -> None:
    result = await run(FakeGitHub([check()]))

    assert result.outcome is CiOutcome.SUCCESS
    assert result.succeeded is True
    assert "CI passed" in result.summary


async def test_a_red_check_fails_and_names_the_check() -> None:
    result = await run(
        FakeGitHub([{"name": "lint", "status": "completed", "conclusion": "failure"}])
    )

    assert result.outcome is CiOutcome.FAILURE
    assert "lint" in result.summary


async def test_it_waits_for_an_incomplete_check() -> None:
    github = FakeGitHub([check(status="in_progress", conclusion=None)], [check()])

    result = await run(github)

    assert result.outcome is CiOutcome.SUCCESS
    assert github.polls == 2


async def test_skipped_and_neutral_conclusions_count_as_success() -> None:
    result = await run(FakeGitHub([check(conclusion="skipped"), check(conclusion="neutral")]))

    assert result.outcome is CiOutcome.SUCCESS


async def test_one_failure_among_many_fails_the_whole_ref() -> None:
    result = await run(FakeGitHub([check(), check(conclusion="failure")]))

    assert result.outcome is CiOutcome.FAILURE


async def test_no_checks_at_all_is_never_success() -> None:
    # A repository with no CI has verified nothing, so the pull request stays in
    # draft rather than being marked ready on the strength of a sandbox run.
    result = await run(FakeGitHub([]), timeout_minutes=0)

    assert result.outcome in {CiOutcome.NO_CHECKS, CiOutcome.TIMED_OUT}
    assert result.succeeded is False
    assert (
        "nothing independent verified this" in result.summary or "did not finish" in result.summary
    )


async def test_a_timeout_is_not_a_pass() -> None:
    result = await run(FakeGitHub([check(status="queued", conclusion=None)]), timeout_minutes=0)

    assert result.outcome is CiOutcome.TIMED_OUT
    assert result.succeeded is False


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", None])
async def test_any_non_passing_conclusion_fails(conclusion: str | None) -> None:
    result = await run(FakeGitHub([check(conclusion=conclusion)]))

    assert result.succeeded is False
