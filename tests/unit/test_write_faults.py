"""Application failures on every write, and what the applications hold afterwards.

The question is never "did it retry". It is: once the run stopped, how many
issues, pull requests, and replies exist, and is the run either finished or
honestly resumable. Three failure shapes matter and behave differently:

- rejected before applying (a 429): retrying is safe;
- applied, then the response was lost (a timeout after the write landed):
  retrying blindly writes twice, so the application must be asked first;
- unavailable for longer than the retries last: the run pauses, and resume
  finishes it without repeating anything that already landed.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome
from proofpr.domain.errors import PermanentAppError, RetryableAppError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Pipeline
from tests.conftest import REPO_ROOT
from tests.fixtures.sandboxes import PATCH_AND_PROOF_SUCCEED, ScriptedSandbox
from tests.unit.test_pipeline_patch import REPORT
from tests.unit.test_resume import Apps, build, report

#: Every write a successful run makes against the sample repository.
CREATES = [
    "github.create_branch",
    "github.push",
    "github.open_pr",
    "linear.create_issue",
    "discord.reply",
    "linear.attach_url",
    "linear.comment",
]
IDEMPOTENT = ["github.mark_ready"]
EVERY_WRITE = CREATES + IDEMPOTENT


def rate_limited(operation: str) -> RetryableAppError:
    return RetryableAppError(f"429 on {operation}", app=operation.split(".")[0], status=429)


def timed_out(operation: str) -> RetryableAppError:
    return RetryableAppError(f"timeout on {operation}", app=operation.split(".")[0])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    destination = tmp_path / "validkit"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "validkit", destination)
    return destination


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


@pytest.fixture
def apps(guard: Any) -> Apps:
    return Apps(
        guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
    )


def quick(apps: Apps, ledger: Ledger, repo: Path) -> Pipeline:
    """A pipeline that retries without sleeping."""
    pipeline = build(apps, ledger, repo)
    pipeline.config = replace(pipeline.config, write_backoff_seconds=0.0)
    return pipeline


def fake_for(apps: Apps, operation: str) -> Any:
    return getattr(apps, operation.split(".")[0])


def assert_written_once(apps: Apps) -> None:
    """Exactly one of everything a successful run creates."""
    assert len(apps.linear.issues) == 1
    assert len(apps.github.pulls) == 1
    assert len(apps.github.branches) == 2  # main, plus this run's branch
    replies = [m for m in apps.discord.messages.values() if m.get("author", {}).get("id") == "bot"]
    assert len(replies) == 1
    issue = next(iter(apps.linear.issues.values()))
    assert len(issue["attachments"]["nodes"]) == 1
    receipts = [n for n in issue["comments"]["nodes"] if "Receipt" in n["body"]]
    assert len(receipts) == 1


def kinds(ledger: Ledger, run_id: str) -> list[str]:
    return [event["kind"] for event in ledger.events(run_id)]


class TestRejectedBeforeApplying:
    """A transient refusal is retried, and the run finishes as if nothing happened."""

    @pytest.mark.parametrize("operation", EVERY_WRITE)
    async def test_one_rate_limit_is_absorbed(
        self, apps: Apps, ledger: Ledger, repo: Path, operation: str
    ) -> None:
        fake_for(apps, operation).inject(operation, rate_limited(operation))

        state = await quick(apps, ledger, repo).run(report())

        assert state.outcome is Outcome.PR_OPENED, state.reason
        assert state.consistent is True
        assert_written_once(apps)
        assert "write_retry" in kinds(ledger, state.run_id)

    async def test_a_permanent_refusal_is_not_retried(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        apps.github.inject(
            "github.open_pr", PermanentAppError("403 forbidden", app="github", status=403)
        )

        state = await quick(apps, ledger, repo).run(report())

        assert state.outcome is Outcome.FAILED_CLOSED
        assert "write_retry" not in kinds(ledger, state.run_id)
        assert apps.github.pulls == {}


class TestAppliedButResponseLost:
    """The change landed and nobody heard back. It must not land twice."""

    @pytest.mark.parametrize("operation", CREATES)
    async def test_a_lost_response_is_recovered_not_repeated(
        self, apps: Apps, ledger: Ledger, repo: Path, operation: str
    ) -> None:
        fake_for(apps, operation).inject(operation, timed_out(operation), after_apply=True)

        state = await quick(apps, ledger, repo).run(report())

        assert state.outcome is Outcome.PR_OPENED, state.reason
        assert state.consistent is True
        assert_written_once(apps)
        assert "write_recovered" in kinds(ledger, state.run_id)

    async def test_a_crash_after_applying_is_recovered_on_resume(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # The process dies after the issue exists and before the ledger heard
        # about it. Only the recorded attempt tells resume to look first.
        class Crash(Exception):
            pass

        apps.linear.inject("linear.create_issue", Crash("died"), after_apply=True)
        pipeline = quick(apps, ledger, repo)
        with pytest.raises(Crash):
            await pipeline.run(report())
        run_id = ledger.unfinished_runs()[0]["run_id"]

        state = await pipeline.resume(run_id)

        assert state.outcome is Outcome.PR_OPENED
        assert_written_once(apps)
        assert "write_recovered" in kinds(ledger, run_id)

    async def test_a_receipt_is_not_mistaken_for_another_comment_on_the_same_issue(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # Two comments by one run on one issue carry the same run marker. A
        # lookup on the marker alone would find the first and skip the second.
        apps.linear.inject("linear.comment", timed_out("linear.comment"), after_apply=False)

        state = await quick(apps, ledger, repo).run(report())

        issue = next(iter(apps.linear.issues.values()))
        assert any("Receipt" in node["body"] for node in issue["comments"]["nodes"])
        assert state.outcome is Outcome.PR_OPENED


class TestUnavailableForLonger:
    """Retries run out. The run pauses, and resume finishes it cleanly."""

    @pytest.mark.parametrize("operation", EVERY_WRITE)
    async def test_exhausted_retries_pause_the_run_instead_of_closing_it(
        self, apps: Apps, ledger: Ledger, repo: Path, operation: str
    ) -> None:
        fake_for(apps, operation).inject(operation, rate_limited(operation), times=10)
        pipeline = quick(apps, ledger, repo)

        paused = await pipeline.run(report())

        assert paused.outcome is None
        assert ledger.get_run(paused.run_id)["finished_at"] is None  # type: ignore[index]
        assert "run_paused" in kinds(ledger, paused.run_id)

        fake_for(apps, operation).faults.clear()
        state = await pipeline.resume(paused.run_id)

        assert state.outcome is Outcome.PR_OPENED, state.reason
        assert state.consistent is True
        assert_written_once(apps)
        verified, detail = ledger.verify_receipt(paused.run_id)
        assert verified, detail

    async def test_a_decline_interrupted_while_filing_finishes_as_a_decline(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = quick(apps, ledger, repo)
        apps.linear.inject("linear.create_issue", rate_limited("linear.create_issue"), times=10)
        unversioned = Report(
            source="discord",
            text=REPORT.replace("Running version 0.4.1.", ""),
            author="mira",
            channel_id=1,
            message_id=2,
        )

        paused = await pipeline.run(unversioned)
        apps.linear.faults.clear()
        state = await pipeline.resume(paused.run_id)

        assert state.outcome is Outcome.OUT_OF_SCOPE
        assert apps.github.pulls == {}
        assert apps.sandbox.calls == []
        assert len(apps.linear.issues) == 1

    async def test_an_application_that_cannot_be_asked_is_never_written_to_again(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # The write's outcome is unknown and the lookup itself fails. Sending
        # again is the one move that can duplicate, so the run pauses instead.
        original = apps.linear.find_existing
        lookups = 0

        async def unreachable(intent: WriteIntent) -> WriteResult | None:
            nonlocal lookups
            lookups += 1
            raise RetryableAppError("linear is down", app="linear", status=503)

        apps.linear.find_existing = unreachable  # type: ignore[method-assign]
        apps.linear.inject(
            "linear.create_issue", timed_out("linear.create_issue"), after_apply=True
        )
        pipeline = quick(apps, ledger, repo)

        paused = await pipeline.run(report())

        assert paused.outcome is None
        assert lookups == 1
        assert len(apps.linear.issues) == 1

        apps.linear.find_existing = original  # type: ignore[method-assign]
        state = await pipeline.resume(paused.run_id)
        assert state.outcome is Outcome.PR_OPENED
        assert_written_once(apps)


class TestBackoff:
    """Retries wait, and the wait grows."""

    async def test_retries_sleep_with_a_doubling_delay(
        self, apps: Apps, ledger: Ledger, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []

        async def record(delay: float) -> None:
            delays.append(delay)

        monkeypatch.setattr("proofpr.pipeline.asyncio.sleep", record)
        apps.github.inject("github.open_pr", rate_limited("github.open_pr"), times=2)
        pipeline = build(apps, ledger, repo)
        pipeline.config = replace(pipeline.config, write_backoff_seconds=0.5)

        state = await pipeline.run(report())

        assert state.outcome is Outcome.PR_OPENED
        assert delays == [0.5, 1.0]


class TestReceiptStability:
    """The receipt is fixed once, not recomputed on every attempt.

    A fault run against real containers found this by accident: `linear.comment
    / crash_after_apply` produced a duplicate. The root cause was not the retry
    logic, it was that the receipt embedded in that comment was recomputed from
    the live chain head on every attempt, so a resumed replay posted the *same*
    receipt comment with a *different* number inside it, and idempotency
    matching by content correctly treated them as two different writes.
    """

    async def test_the_posted_receipt_equals_the_sealed_receipt(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await quick(apps, ledger, repo).run(report())

        issue = next(iter(apps.linear.issues.values()))
        posted = next(
            node["body"] for node in issue["comments"]["nodes"] if "Receipt" in node["body"]
        )
        assert state.receipt is not None
        assert f"sha256:{state.receipt[:32]}" in posted
        verified, detail = ledger.verify_receipt(state.run_id)
        assert verified, detail

    async def test_a_crash_after_the_receipt_comment_lands_posts_the_same_number_twice(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        class Crash(Exception):
            pass

        apps.linear.inject("linear.comment", Crash("died right after"), after_apply=True)
        pipeline = quick(apps, ledger, repo)
        with pytest.raises(Crash):
            await pipeline.run(report())
        run_id = ledger.unfinished_runs()[0]["run_id"]

        state = await pipeline.resume(run_id)

        issue = next(iter(apps.linear.issues.values()))
        receipts = [
            node["body"] for node in issue["comments"]["nodes"] if "Receipt" in node["body"]
        ]
        assert len(receipts) == 1
        assert f"sha256:{state.receipt[:32]}" in receipts[0]  # type: ignore[index]
