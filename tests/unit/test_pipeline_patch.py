"""The pipeline through patching and proof."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome, Step
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Config, Pipeline
from proofpr.steps.patch import PatchedFile, ProposedPatch
from proofpr.steps.test_synth import SynthesizedTest
from proofpr.triage.intent import IntentVerdict
from tests.conftest import REPO_ROOT
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear
from tests.fixtures.sandboxes import PATCH_AND_PROOF_SUCCEED, ScriptedSandbox

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"

REPORT = """\
parse_date crashes on an invalid month instead of raising a clear error.

Traceback (most recent call last):
  File "app.py", line 3, in <module>
    parse_date("2024-13-01")
  File "src/validkit/dates.py", line 30, in parse_date
    if day > length:
TypeError: '>' not supported between instances of 'int' and 'NoneType'

Running version 0.4.1.
"""

VALID_TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
"""


def patched_source() -> str:
    """The sample file with a guard added, so mutation has something to chew on."""
    original = (VALIDKIT / "src" / "validkit" / "dates.py").read_text()
    return original.replace(
        "    if day > length:",
        '    if length is None:\n        raise ValueError(f"month {month} is out of range")\n'
        "    if day > length:",
    )


class ScriptedModel:
    """Returns a test, then patches, recording every schema it was asked for."""

    def __init__(self, *patches: ProposedPatch) -> None:
        """Build the stub."""
        self.patches = list(patches) or [
            ProposedPatch(
                files=[PatchedFile(path="src/validkit/dates.py", content=patched_source())],
                summary="validate the month before comparing lengths",
            )
        ]
        self.calls: list[str] = []

    async def complete_json(
        self, *, system: str, user: str, schema: type[Any], tier: str = "cheap", **kwargs: Any
    ) -> tuple[Any, Any]:
        """Return the next scripted object."""
        self.calls.append(schema.__name__)
        usage = type(
            "Usage",
            (),
            {
                "tier": tier,
                "model": "stub",
                "input_tokens": 500,
                "output_tokens": 200,
                "cost_usd": 0.01,
            },
        )()
        if schema is IntentVerdict:
            return IntentVerdict(intent="bug", confidence=0.95), usage
        if schema is SynthesizedTest:
            return SynthesizedTest(path="tests/test_repro.py", code=VALID_TEST), usage
        if schema is ProposedPatch:
            # The last scripted patch repeats, so a test can script one patch and
            # still exercise a loop that retries.
            return (self.patches.pop(0) if len(self.patches) > 1 else self.patches[0]), usage
        return schema(
            **{n: "stubbed" for n, f in schema.model_fields.items() if f.is_required()}
        ), usage


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


async def run(apps: Apps, ledger: Ledger, repo: Path, model: Any = None) -> Any:
    """Run the pipeline once against the sample repository."""
    config = Config(
        supported_versions=">=0.3.0",
        patch_paths=("src/**",),
        repo_path=repo,
        package="validkit",
    )
    return await Pipeline(
        apps=apps, ledger=ledger, model=model or ScriptedModel(), config=config
    ).run(Report(source="discord", text=REPORT, author="mira", channel_id=1, message_id=2))


class TestSuccessfulProof:
    """A patch that survives every check is filed with its proof."""

    async def test_a_proved_patch_is_not_published_without_an_approver(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        # No approver configured is the safe default: an agent with nobody to ask
        # must not push to someone's repository, however good its proof is.
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.REJECTED_BY_MAINTAINER
        assert state.proof_complete is True
        assert state.pr_url is None
        assert apps.github.pulls == {}
        assert state.patch_attempts == 1
        assert state.mutants_total > 0
        assert state.mutants_killed == state.mutants_total

    async def test_the_proof_block_is_attached_to_the_issue(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        state = await run(apps, ledger, repo)

        description = apps.linear.issues[state.linear_issue_id or ""]["description"]
        assert "## Proof" in description
        assert "Revert patch, rerun new test" in description
        assert "Mutants on patched lines" in description
        assert "validate the month before comparing lengths" in description

    async def test_the_proof_result_is_recorded_in_the_ledger(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        state = await run(apps, ledger, repo)

        events = {event["kind"]: event for event in ledger.events(state.run_id)}
        assert events["proof_result"]["payload"]["complete"] is True
        assert events["proof_result"]["payload"]["killed"] >= 1
        assert events["patch_result"]["payload"]["accepted"] is True

    async def test_the_strong_model_is_used_for_both_the_test_and_the_patch(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        model = ScriptedModel()
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        await run(apps, ledger, repo, model)

        assert model.calls.count("SynthesizedTest") == 1
        assert model.calls.count("ProposedPatch") == 1


class TestIncompleteProof:
    """An unproved patch is never published, whatever the tests say."""

    async def test_a_patch_whose_revert_still_passes_is_abandoned(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        # Gate passes, patch works, but reverting it does not break the test.
        codes = [0, 1, 0, 0, 0]
        apps = Apps(guard, ScriptedSandbox(pytest_exit_codes=codes, default_exit_code=0))

        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.ABANDONED
        assert state.proof_complete is False

    async def test_a_surviving_mutant_stops_publication(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        # Everything passes until the first mutant, which the test fails to kill.
        codes = [*PATCH_AND_PROOF_SUCCEED, 0]
        apps = Apps(guard, ScriptedSandbox(pytest_exit_codes=codes, default_exit_code=1))

        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.ABANDONED
        assert state.mutants_killed < state.mutants_total

    async def test_an_unproved_patch_still_files_the_test_for_a_human(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        codes = [0, 1, 0, 0, 0]
        apps = Apps(guard, ScriptedSandbox(pytest_exit_codes=codes, default_exit_code=0))

        state = await run(apps, ledger, repo)

        description = apps.linear.issues[state.linear_issue_id or ""]["description"]
        assert "Failing test" in description
        assert "Captured traceback" in description


class TestFailedPatchLoop:
    """Three failed attempts end the run with the test still worth filing."""

    async def test_a_patch_that_never_works_is_abandoned(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        # Gate passes, then every patch attempt leaves the test failing.
        apps = Apps(guard, ScriptedSandbox(pytest_exit_codes=[0, 1], default_exit_code=1))

        state = await run(apps, ledger, repo)

        assert state.outcome is Outcome.ABANDONED
        assert state.patch_attempts == 3

    async def test_a_patch_aimed_at_a_forbidden_path_is_refused(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        forbidden = ProposedPatch(
            files=[PatchedFile(path="pytest.ini", content="[pytest]\n")], summary="nope"
        )
        model = ScriptedModel(forbidden, forbidden, forbidden)
        apps = Apps(guard, ScriptedSandbox(pytest_exit_codes=[0, 1], default_exit_code=0))

        state = await run(apps, ledger, repo, model)

        assert state.outcome is Outcome.ABANDONED
        events = {event["kind"]: event for event in ledger.events(state.run_id)}
        attempts = events["patch_result"]["payload"]["attempts"]
        assert all(attempt["accepted"] is False for attempt in attempts)
        assert "out_of_allowed_paths" in attempts[0]["why"]


class TestWorktreeIsolation:
    """The real checkout is never touched, patched or not."""

    async def test_the_repository_is_unchanged_after_a_successful_patch(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        before = (repo / "src" / "validkit" / "dates.py").read_text()
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        await run(apps, ledger, repo)

        assert (repo / "src" / "validkit" / "dates.py").read_text() == before
        assert not (repo / "tests" / "test_repro.py").exists()

    async def test_the_patch_step_is_bracketed_in_the_ledger(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        apps = Apps(
            guard, ScriptedSandbox(pytest_exit_codes=PATCH_AND_PROOF_SUCCEED, default_exit_code=1)
        )

        state = await run(apps, ledger, repo)

        steps = [e["step"] for e in ledger.events(state.run_id) if e["kind"] == "step_started"]
        assert Step.PATCH.value in steps
