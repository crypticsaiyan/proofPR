"""Release notification, regression watch, and the drift reconciler."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Config, Pipeline
from proofpr.post import Reconciler, ReleaseNotifier, run_id_from_body
from proofpr.steps.approve import AutoApprover
from tests.conftest import REPO_ROOT
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear
from tests.fixtures.sandboxes import PATCH_AND_PROOF_SUCCEED, ScriptedSandbox
from tests.unit.test_pipeline_patch import REPORT, ScriptedModel

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"


class Apps:
    """Fakes plus a scripted sandbox."""

    def __init__(self, guard: Any) -> None:
        """Build the bundle."""
        self.discord = FakeDiscord(guard)
        self.github = FakeGitHub(guard)
        self.linear = FakeLinear(guard)
        self.sandbox = ScriptedSandbox(
            pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1
        )
        self.guard = guard


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the sample repository."""
    destination = tmp_path / "validkit"
    shutil.copytree(VALIDKIT, destination)
    return destination


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """A ledger over a temporary file."""
    return Ledger(tmp_path / "ledger.db")


@pytest.fixture
def apps(guard: Any) -> Apps:
    """Fakes ready for a full run."""
    return Apps(guard)


async def published(apps: Apps, ledger: Ledger, repo: Path) -> Any:
    """Run the pipeline to a published pull request, and return the state."""
    pipeline = Pipeline(
        apps=apps,
        ledger=ledger,
        model=ScriptedModel(),
        approver=AutoApprover(),
        config=Config(
            supported_versions=">=0.3.0",
            patch_paths=("src/**",),
            repo_path=repo,
            package="validkit",
            ci_poll_seconds=0,
            ci_timeout_minutes=1,
        ),
    )
    state = await pipeline.run(
        Report(source="discord", text=REPORT, author="mira", channel_id=1, message_id=2)
    )
    assert state.outcome is Outcome.PR_OPENED, state.reason
    return state


class TestReleaseNotification:
    """The loop nobody closes by hand."""

    def test_the_run_is_identified_from_the_pull_request_body(self) -> None:
        assert run_id_from_body("...<!-- proofpr-run:r-7f3a -->") == "r-7f3a"
        assert run_id_from_body("someone else's pull request") is None
        assert run_id_from_body(None) is None

    async def test_a_merge_tells_the_reporter_where_it_landed(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)
        before = len(apps.discord.messages)

        notification = await ReleaseNotifier(apps=apps, ledger=ledger).on_merged(
            run_id=state.run_id, merge_ref="abc1234"
        )

        assert notification.told_reporter is True
        assert len(apps.discord.messages) == before + 1
        newest = apps.discord.messages[max(apps.discord.messages, key=int)]["content"]
        assert "fixed and merged" in newest
        assert "abc1234" in newest

    async def test_the_merge_is_recorded_in_the_ledger(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)

        await ReleaseNotifier(apps=apps, ledger=ledger).on_merged(run_id=state.run_id)

        kinds = [event["kind"] for event in ledger.events(state.run_id)]
        assert "fix_merged" in kinds
        assert "release_notified" in kinds

    async def test_a_webhook_for_someone_elses_pull_request_is_ignored(
        self, apps: Apps, ledger: Ledger
    ) -> None:
        notifier = ReleaseNotifier(apps=apps, ledger=ledger)

        result = await notifier.on_pull_request_event(
            {"action": "closed", "pull_request": {"merged": True, "body": "not ours"}}
        )

        assert result is None

    async def test_a_closed_but_unmerged_pull_request_announces_nothing(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)
        notifier = ReleaseNotifier(apps=apps, ledger=ledger)

        result = await notifier.on_pull_request_event(
            {
                "action": "closed",
                "pull_request": {
                    "merged": False,
                    "body": f"<!-- proofpr-run:{state.run_id} -->",
                },
            }
        )

        assert result is None

    async def test_an_unknown_run_is_reported_rather_than_crashing(
        self, apps: Apps, ledger: Ledger
    ) -> None:
        notification = await ReleaseNotifier(apps=apps, ledger=ledger).on_merged(run_id="r-nope")

        assert notification.told_reporter is False
        assert "no state recorded" in notification.detail


class TestReconciler:
    """Drift is repaired, and only where the run already reached a conclusion."""

    async def test_a_consistent_run_needs_no_repair(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        await published(apps, ledger, repo)

        report = await Reconciler(apps=apps, ledger=ledger).run_once()

        assert report.checked == 1
        assert report.diverged == 0
        assert report.repairs == []

    async def test_a_deleted_reply_is_re_sent(self, apps: Apps, ledger: Ledger, repo: Path) -> None:
        state = await published(apps, ledger, repo)
        apps.discord.messages.pop(state.discord_reply_id)

        report = await Reconciler(apps=apps, ledger=ledger).run_once()

        assert report.diverged == 1
        assert report.repaired == 1
        assert any("re-sent" in repair.detail for repair in report.repairs)

    async def test_a_rewritten_issue_has_its_record_restored(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)
        apps.linear.issues[state.linear_issue_id]["description"] = "someone rewrote this"
        apps.linear.issues[state.linear_issue_id]["comments"]["nodes"].clear()
        reconciler = Reconciler(apps=apps, ledger=ledger)

        report = await reconciler.run_once()

        assert report.repaired >= 1
        assert any("record re-applied" in repair.detail for repair in report.repairs)
        # And the repair actually holds: a second sweep finds nothing wrong.
        assert (await reconciler.run_once()).diverged == 0

    async def test_a_missing_pull_request_is_left_for_a_human(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # Re-opening a pull request is a new decision, and the reconciler makes
        # none. It re-applies conclusions the run already reached.
        await published(apps, ledger, repo)
        apps.github.pulls.clear()

        report = await Reconciler(apps=apps, ledger=ledger).run_once()

        assert report.unrepaired
        assert any("needs a human" in repair.detail for repair in report.unrepaired)

    async def test_every_repair_is_recorded_in_the_ledger(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)
        apps.discord.messages.pop(state.discord_reply_id)

        await Reconciler(apps=apps, ledger=ledger).run_once()

        reconciled = [e for e in ledger.events(state.run_id) if e["kind"] == "reconciled"]
        assert reconciled
        assert reconciled[0]["payload"]["repaired"] is True

    async def test_an_unreachable_application_does_not_abort_the_sweep(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await published(apps, ledger, repo)

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("github is down")

        apps.github.get_pr = broken  # type: ignore[method-assign]

        report = await Reconciler(apps=apps, ledger=ledger).run_once()

        assert report.checked == 1
        assert report.unrepaired
        assert state.run_id in {repair.run_id for repair in report.repairs}
