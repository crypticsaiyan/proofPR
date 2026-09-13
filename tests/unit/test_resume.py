"""Crash and resume: a run that dies mid-step finishes without writing twice.

A crash is simulated by raising from inside a step, which is what a real crash
looks like to everything downstream of the ledger: the step started, nothing
finished it, and the process stopped. A real SIGKILL is exercised separately in
`tests/integration`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome, Step
from proofpr.domain.run import Report
from proofpr.ledger import Ledger, LedgerError
from proofpr.pipeline import Config, Pipeline
from proofpr.steps.approve import AutoApprover
from tests.conftest import REPO_ROOT
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear
from tests.fixtures.sandboxes import PATCH_AND_PROOF_SUCCEED, ScriptedSandbox
from tests.unit.test_pipeline_patch import REPORT, ScriptedModel

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"


class Crash(Exception):
    """A simulated process death. Deliberately not a ProofPRError."""


class Apps:
    """Fakes that survive the crash, as real applications would."""

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
    """A ledger that outlives the crash, as a real one would."""
    return Ledger(tmp_path / "ledger.db")


@pytest.fixture
def apps(guard: Any) -> Apps:
    """Fakes with a sandbox scripted for a successful patch and proof."""
    return Apps(
        guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
    )


def build(apps: Apps, ledger: Ledger, repo: Path) -> Pipeline:
    """Build a pipeline over the sample repository."""
    return Pipeline(
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


def report() -> Report:
    """The report every test in this module uses."""
    return Report(source="discord", text=REPORT, author="mira", channel_id=1, message_id=2)


def crash_at(pipeline: Pipeline, step: Step) -> Callable[[], None]:
    """Make one step die, and return a function that restores it."""
    name = {
        Step.SANITIZE: "_sanitize",
        Step.PRE_CHECK: "_pre_check",
        Step.FINGERPRINT: "_fingerprint_report",
        Step.EXISTS: "_exists",
        Step.WORTH_IT: "_worth_it",
        Step.REPRO_RAW: "_reproduce",
        Step.PATCH: "_patch_and_prove",
        Step.APPROVAL: "_approve",
        Step.PUBLISH: "_publish",
        Step.CI_WAIT: "_wait_for_ci",
    }[step]
    original = getattr(pipeline, name)

    async def dying(*args: Any, **kwargs: Any) -> Any:
        raise Crash(f"process died inside {step.value}")

    setattr(pipeline, name, dying)
    return lambda: setattr(pipeline, name, original)


CRASH_POINTS = [
    Step.PRE_CHECK,
    Step.FINGERPRINT,
    Step.EXISTS,
    Step.WORTH_IT,
    Step.REPRO_RAW,
    Step.PATCH,
    Step.APPROVAL,
    Step.PUBLISH,
    Step.CI_WAIT,
]


class TestResumeFromEveryStep:
    """Dying in any step is recoverable, and recovery writes nothing twice."""

    @pytest.mark.parametrize("step", CRASH_POINTS, ids=lambda step: step.value)
    async def test_a_crash_is_resumable_and_reaches_the_same_outcome(
        self, apps: Apps, ledger: Ledger, repo: Path, step: Step
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, step)

        with pytest.raises(Crash):
            await pipeline.run(report())

        assert ledger.resume_point(ledger.unfinished_runs()[0]["run_id"]) is step
        restore()
        state = await pipeline.resume(ledger.unfinished_runs()[0]["run_id"])

        assert state.outcome is Outcome.PR_OPENED, state.reason
        assert state.consistent is True

    @pytest.mark.parametrize("step", CRASH_POINTS, ids=lambda step: step.value)
    async def test_a_resumed_run_never_writes_twice(
        self, apps: Apps, ledger: Ledger, repo: Path, step: Step
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, step)
        with pytest.raises(Crash):
            await pipeline.run(report())
        restore()

        await pipeline.resume(ledger.unfinished_runs()[0]["run_id"])

        assert len(apps.linear.issues) == 1
        assert len(apps.github.pulls) == 1
        assert len(apps.github.branches) == 2  # main, plus this run's branch
        operations = [operation for operation, _ in apps.github.calls]
        assert operations.count("github.create_branch") == 1
        assert operations.count("github.open_pr") == 1

    @pytest.mark.parametrize("step", CRASH_POINTS, ids=lambda step: step.value)
    async def test_the_chain_survives_the_crash(
        self, apps: Apps, ledger: Ledger, repo: Path, step: Step
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, step)
        with pytest.raises(Crash):
            await pipeline.run(report())

        run_id = ledger.unfinished_runs()[0]["run_id"]
        assert ledger.verify_chain(run_id) == (True, None)

        restore()
        await pipeline.resume(run_id)
        verified, detail = ledger.verify_receipt(run_id)
        assert verified, detail


class TestResumeMechanics:
    """Resume skips what finished and refuses what it cannot do."""

    async def test_finished_steps_are_not_run_again(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, Step.PUBLISH)
        with pytest.raises(Crash):
            await pipeline.run(report())
        restore()
        run_id = ledger.unfinished_runs()[0]["run_id"]
        before = len(apps.sandbox.calls)

        await pipeline.resume(run_id)

        # Reproduction and proof already finished, so no sandbox work repeats.
        assert len(apps.sandbox.calls) == before

    async def test_the_resume_is_recorded_in_the_ledger(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, Step.EXISTS)
        with pytest.raises(Crash):
            await pipeline.run(report())
        restore()
        run_id = ledger.unfinished_runs()[0]["run_id"]

        await pipeline.resume(run_id)

        resumed = [e for e in ledger.events(run_id) if e["kind"] == "resumed"]
        assert len(resumed) == 1
        # State comes from the last step that finished; execution re-enters the
        # step that did not.
        assert resumed[0]["payload"]["restored_after"] == Step.FINGERPRINT.value
        assert resumed[0]["payload"]["reentering"] == Step.EXISTS.value
        assert Step.SANITIZE.value in resumed[0]["payload"]["completed"]

    async def test_resuming_an_unknown_run_is_an_error(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        with pytest.raises(LedgerError, match="no run"):
            await build(apps, ledger, repo).resume("r-nope")

    async def test_resuming_a_finished_run_is_refused(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = build(apps, ledger, repo)
        state = await pipeline.run(report())

        with pytest.raises(LedgerError, match="already finished"):
            await pipeline.resume(state.run_id)

    async def test_a_crash_before_any_step_finished_cannot_be_resumed(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = build(apps, ledger, repo)
        crash_at(pipeline, Step.SANITIZE)
        with pytest.raises(Crash):
            await pipeline.run(report())

        with pytest.raises(LedgerError, match="no completed step"):
            await pipeline.resume(ledger.unfinished_runs()[0]["run_id"])

    async def test_a_double_resume_is_harmless(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        pipeline = build(apps, ledger, repo)
        restore = crash_at(pipeline, Step.CI_WAIT)
        with pytest.raises(Crash):
            await pipeline.run(report())
        restore()
        run_id = ledger.unfinished_runs()[0]["run_id"]

        first = await pipeline.resume(run_id)
        with pytest.raises(LedgerError):
            await pipeline.resume(run_id)

        assert first.outcome is Outcome.PR_OPENED
        assert len(apps.github.pulls) == 1


class TestResumeAfterAStop:
    """A run that decided to stop stays stopped, however it died afterwards."""

    async def test_a_crash_while_filing_a_decline_does_not_resume_past_the_decision(
        self, apps: Apps, ledger: Ledger, repo: Path
    ) -> None:
        # The worth-it check stops this report (no version stated). The process
        # then dies while filing the issue. Resuming must finish the decline,
        # never carry on into reproduction and patching.
        pipeline = build(apps, ledger, repo)
        apps.linear.fail_next["linear.create_issue"] = Crash("died filing the issue")
        unversioned = Report(
            source="discord",
            text=REPORT.replace("Running version 0.4.1.", ""),
            author="mira",
            channel_id=1,
            message_id=2,
        )

        with pytest.raises(Crash):
            await pipeline.run(unversioned)
        run_id = ledger.unfinished_runs()[0]["run_id"]

        state = await pipeline.resume(run_id)

        assert state.outcome is Outcome.OUT_OF_SCOPE
        assert apps.github.pulls == {}
        assert apps.sandbox.calls == []
        assert len(apps.linear.issues) == 1
