"""The pipeline through publication: branch, pull request, CI, four-way verify."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome, Step
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Config, Pipeline
from proofpr.steps.approve import Approval, AutoApprover
from tests.conftest import REPO_ROOT
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear
from tests.fixtures.sandboxes import PATCH_AND_PROOF_SUCCEED, ScriptedSandbox
from tests.unit.test_pipeline_patch import REPORT, ScriptedModel

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"


class Rejecting:
    """An approver that says no, with a reason."""

    async def request(self, request: Any) -> Approval:
        """Refuse."""
        return Approval(approved=False, decided_by="maintainer", reason="I want to look first")


class Exploding:
    """An approver that cannot be reached."""

    async def request(self, request: Any) -> Approval:
        """Fail."""
        raise ConnectionError("the approval service is down")


class Apps:
    """Fakes plus a scripted sandbox."""

    def __init__(self, guard: Any, sandbox: ScriptedSandbox) -> None:
        """Build the bundle."""
        self.discord = FakeDiscord(guard)
        self.github = FakeGitHub(guard)
        self.linear = FakeLinear(guard)
        self.sandbox = sandbox
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
    """Fakes with a sandbox scripted for a successful patch and proof."""
    return Apps(
        guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
    )


async def run(apps: Apps, ledger: Ledger, repo: Path, approver: Any = None) -> Any:
    """Run the pipeline once, approving automatically unless told otherwise."""
    config = Config(
        supported_versions=">=0.3.0",
        patch_paths=("src/**",),
        repo_path=repo,
        package="validkit",
        ci_poll_seconds=0,
        ci_timeout_minutes=1,
    )
    return await Pipeline(
        apps=apps,
        ledger=ledger,
        model=ScriptedModel(),
        config=config,
        approver=approver or AutoApprover(),
    ).run(Report(source="discord", text=REPORT, author="mira", channel_id=1, message_id=2))


class TestPullRequest:
    """A proved patch becomes a draft pull request that leaves draft on green CI."""

    async def test_the_run_ends_with_a_pull_request_open(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.PR_OPENED
        assert state.pr_number is not None
        assert state.branch == f"proofpr/{state.run_id}"
        assert state.pr_ready is True

    async def test_the_pull_request_carries_the_proof_block_and_the_marker(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        body = apps.github.pulls[str(state.pr_number)]["body"]
        assert body.startswith("## Proof")
        assert "Revert patch, rerun new test" in body
        assert "Mutants on patched lines" in body
        assert f"proofpr-run:{state.run_id}" in body

    async def test_the_test_is_committed_before_the_fix(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # The branch history should show the failure before the fix, so a
        # reviewer can check out the first commit and watch it fail.
        await run(apps, ledger, repo)

        pushes = [call for call in apps.github.calls if call[0] == "github.push"]
        assert [call[1]["path"] for call in pushes] == [
            "tests/test_repro.py",
            "src/validkit/dates.py",
        ]
        assert pushes[0][1]["message"].startswith("test:")
        assert pushes[1][1]["message"].startswith("fix:")

    async def test_the_branch_carries_the_run_id(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        assert state.run_id in (state.branch or "")
        assert state.branch in apps.github.branches

    async def test_the_pull_request_is_attached_to_the_issue(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        attachments = apps.linear.issues[state.linear_issue_id or ""]["attachments"]["nodes"]
        assert [node["url"] for node in attachments] == [state.pr_url]


class TestCi:
    """CI is read from the API, and absence of checks is never success."""

    async def test_a_red_check_leaves_the_pull_request_in_draft(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        apps.github.check_conclusion = "failure"

        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.CI_FAILED
        assert state.pr_ready is False
        assert apps.github.pulls[str(state.pr_number)]["draft"] is True
        assert "CI failed" in state.ci_summary

    async def test_a_green_check_takes_it_out_of_draft(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        assert apps.github.pulls[str(state.pr_number)]["draft"] is False
        assert "CI passed" in state.ci_summary

    async def test_the_ci_result_is_recorded_in_the_ledger(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        events = {event["kind"]: event for event in ledger.events(state.run_id)}
        assert events["ci_result"]["payload"]["outcome"] == "success"


class TestApproval:
    """Publication needs someone to say yes, and any failure means no."""

    async def test_a_refusal_stops_publication_and_says_why(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo, Rejecting())

        assert state.outcome is Outcome.REJECTED_BY_MAINTAINER
        assert apps.github.pulls == {}
        assert apps.github.branches == {"main": "base-sha"}

    async def test_an_unreachable_approver_is_treated_as_a_refusal(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo, Exploding())

        assert state.outcome is Outcome.REJECTED_BY_MAINTAINER
        assert apps.github.pulls == {}

    async def test_the_decision_and_its_author_are_recorded(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        events = {event["kind"]: event for event in ledger.events(state.run_id)}
        assert events["approval_received"]["payload"] == {
            "approved": True,
            "by": "auto",
            "why": "",
        }
        assert state.approved_by == "auto"


class TestFourWayVerification:
    """All four applications must agree with the run's terminal state."""

    async def test_a_published_run_is_consistent_everywhere(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        assert state.consistent is True
        assert state.consistency_problems == []

    async def test_the_verdict_is_recorded_per_application(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        events = {event["kind"]: event for event in ledger.events(state.run_id)}
        checks = events["four_way_verified"]["payload"]["checks"]
        assert checks == {"github": True, "linear": True, "discord": True}

    async def test_a_missing_reply_is_reported_as_divergence(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # Deleting the reply after it was written is exactly the drift the
        # four-way check exists to notice.
        state = await run(apps, ledger, repo)
        apps.discord.messages.clear()

        from proofpr.steps.verify import assert_consistent

        again = await assert_consistent(
            outcome=Outcome.PR_OPENED,
            run_id=state.run_id,
            github=apps.github,
            linear=apps.linear,
            discord=apps.discord,
            pr_number=state.pr_number,
            linear_issue_id=state.linear_issue_id,
            discord_message_id=state.discord_reply_id,
            discord_channel_id=1,
            expect_ready=True,
        )

        assert again.consistent is False
        assert any("discord" in problem for problem in again.problems)

    async def test_a_non_publishing_outcome_must_leave_no_pull_request(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo, Rejecting())

        assert state.consistent is True
        assert state.pr_number is None

    async def test_the_verify_step_runs_for_every_outcome(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo, Rejecting())

        kinds = [event["kind"] for event in ledger.events(state.run_id)]
        assert "four_way_verified" in kinds


class TestLedgerShape:
    """The full path is bracketed, costed, and sealed."""

    async def test_every_published_step_is_bracketed(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        started = [e["step"] for e in ledger.events(state.run_id) if e["kind"] == "step_started"]
        for step in (Step.APPROVAL, Step.PUBLISH, Step.CI_WAIT):
            assert step.value in started

    async def test_the_receipt_verifies_after_publication(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        assert state.receipt
        assert ledger.verify_chain(state.run_id) == (True, None)

    async def test_the_reporter_is_told_where_the_pull_request_is(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        state = await run(apps, ledger, repo)

        reply = apps.discord.messages[state.discord_reply_id or ""]["content"]
        assert state.pr_url in reply
        assert "CI passed" in reply
