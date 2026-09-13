"""The pipeline through reproduction, with a scripted sandbox and model."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.domain.enums import Outcome, Reason, Step
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Config, Pipeline
from proofpr.steps.patch import PatchedFile, ProposedPatch
from proofpr.steps.test_synth import SynthesizedTest
from proofpr.triage.intent import IntentVerdict
from tests.conftest import REPO_ROOT
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear
from tests.fixtures.sandboxes import GATE_PASSES, ScriptedSandbox

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

CRASH_STDERR = """\
Traceback (most recent call last):
  File "proofpr_repro.py", line 12, in <module>
    result = parse_date("2024-13-01")
  File "src/validkit/dates.py", line 30, in parse_date
    if day > length:
TypeError: '>' not supported between instances of 'int' and 'NoneType'
"""

VALID_TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
"""


class ScriptedModel:
    """A model returning scripted objects, recording every schema requested."""

    def __init__(
        self,
        tests: list[SynthesizedTest] | None = None,
        patch: ProposedPatch | None = None,
    ) -> None:
        """Build the stub."""
        self.tests = list(tests or [])
        self.patch = patch or ProposedPatch(
            files=[
                PatchedFile(
                    path="src/validkit/dates.py",
                    content=(VALIDKIT / "src" / "validkit" / "dates.py").read_text(),
                )
            ],
            summary="validate the month before comparing",
        )
        self.calls: list[str] = []

    async def complete_json(
        self, *, system: str, user: str, schema: type[Any], tier: str = "cheap", **kwargs: Any
    ) -> tuple[Any, Any]:
        """Return the next scripted object for the requested schema."""
        self.calls.append(schema.__name__)
        usage = type(
            "Usage",
            (),
            {
                "tier": tier,
                "model": "stub",
                "input_tokens": 500,
                "output_tokens": 200,
                "cost_usd": 0.002,
            },
        )()
        if schema is IntentVerdict:
            return IntentVerdict(intent="bug", confidence=0.95), usage
        if schema is ProposedPatch:
            return self.patch, usage
        if schema is SynthesizedTest:
            if self.tests:
                return self.tests.pop(0), usage
            return SynthesizedTest(path="tests/test_repro.py", code=VALID_TEST), usage
        # Anything else is a small ad hoc schema defined at its call site, such
        # as the clarifying question. Fill whatever it requires with a marker.
        required = {
            name: "stubbed" for name, field in schema.model_fields.items() if field.is_required()
        }
        return schema(**required), usage


class Apps:
    """The four fakes plus a scripted sandbox."""

    def __init__(self, guard: Any, sandbox: ScriptedSandbox) -> None:
        """Build the bundle."""
        self.discord = FakeDiscord(guard)
        self.github = FakeGitHub(guard)
        self.linear = FakeLinear(guard)
        self.sandbox = sandbox
        self.guard = guard


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the sample repository, outside the source tree."""
    destination = tmp_path / "validkit"
    shutil.copytree(VALIDKIT, destination)
    return destination


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """A ledger over a temporary file."""
    return Ledger(tmp_path / "ledger.db")


def config(repo: Path) -> Config:
    """Configuration pointing at the sample repository."""
    return Config(
        supported_versions=">=0.3.0",
        patch_paths=("src/**",),
        repo_path=repo,
        package="validkit",
    )


def report(text: str = REPORT) -> Report:
    """Build a report."""
    return Report(source="discord", text=text, author="mira", channel_id=1, message_id=2)


async def run(apps: Apps, ledger: Ledger, repo: Path, model: Any) -> Any:
    """Run the pipeline once."""
    return await Pipeline(apps=apps, ledger=ledger, model=model, config=config(repo)).run(report())


class TestReproductionPath:
    """Stage 1 confirms, stage 2 writes a test, the gate accepts it."""

    async def test_a_reproduced_bug_with_a_passing_gate_is_triaged(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(pytest_exit_codes=GATE_PASSES)
        apps = Apps(guard, sandbox)

        state = await run(apps, ledger, repo, ScriptedModel())

        assert state.repro_outcome == "confirmed"
        assert state.repro_exception == "TypeError"
        assert state.gate_passed is True
        assert state.test_path == "tests/test_repro.py"

    async def test_the_failing_test_and_traceback_are_attached_to_the_issue(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(pytest_exit_codes=GATE_PASSES)
        apps = Apps(guard, sandbox)

        state = await run(apps, ledger, repo, ScriptedModel())

        description = apps.linear.issues[state.linear_issue_id or ""]["description"]
        assert "Captured traceback" in description
        assert "Failing test" in description
        assert "test_invalid_month_raises_value_error" in description

    async def test_the_strong_model_is_only_reached_after_a_real_crash(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(
            script={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        )
        model = ScriptedModel()

        await run(Apps(guard, sandbox), ledger, repo, model)

        assert "SynthesizedTest" not in model.calls


class TestClarify:
    """A report that does not reproduce gets one precise question."""

    async def test_no_crash_ends_the_run_with_a_question(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(
            script={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        )
        apps = Apps(guard, sandbox)

        state = await run(apps, ledger, repo, None)

        assert state.outcome is Outcome.DECLINED
        assert state.reason is Reason.REPRO_NO_CRASH

    async def test_the_question_reaches_the_reporter_and_the_ledger(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(
            script={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
        )
        apps = Apps(guard, sandbox)

        state = await Pipeline(
            apps=apps, ledger=ledger, model=ScriptedModel(), config=config(repo)
        ).run(report())

        assert state.clarify_question
        kinds = [event["kind"] for event in ledger.events(state.run_id)]
        assert "clarify_sent" in kinds
        assert state.discord_reply_id is not None

    async def test_a_missing_input_asks_for_the_input(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        apps = Apps(guard, ScriptedSandbox())

        state = await Pipeline(apps=apps, ledger=ledger, model=None, config=config(repo)).run(
            Report(
                source="discord",
                text=(
                    "dates are broken for some months on version 0.4.1\n\n"
                    "Traceback (most recent call last):\n"
                    '  File "src/validkit/dates.py", line 30, in parse_date\n'
                    "    if day > length:\n"
                    "TypeError: bad comparison\n"
                ),
                channel_id=1,
            )
        )

        assert state.reason is Reason.REPRO_MISSING_INPUT
        assert "runnable example" in (state.clarify_question or "")

    async def test_an_environment_failure_is_not_reported_as_a_bug(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        sandbox = ScriptedSandbox(
            script={
                "exit_code": 1,
                "stdout": "",
                "stderr": "ModuleNotFoundError: No module named 'validkit'",
                "timed_out": False,
            }
        )

        state = await run(Apps(guard, sandbox), ledger, repo, None)

        assert state.reason is Reason.REPRO_ENVIRONMENT


class TestConfirmedNoTest:
    """A confirmed crash is worth filing even when no test can be written."""

    async def test_three_invalid_tests_end_as_confirmed_no_test(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        rubbish = SynthesizedTest(path="src/test_wrong.py", code="def test_x():\n    assert False")
        model = ScriptedModel([rubbish, rubbish, rubbish])
        apps = Apps(guard, ScriptedSandbox())

        state = await run(apps, ledger, repo, model)

        assert state.outcome is Outcome.CONFIRMED_NO_TEST
        assert state.test_attempts == 3
        assert model.calls.count("SynthesizedTest") == 3

    async def test_the_traceback_is_filed_even_without_a_test(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        rubbish = SynthesizedTest(path="src/test_wrong.py", code="def test_x():\n    assert False")
        apps = Apps(guard, ScriptedSandbox())

        state = await run(apps, ledger, repo, ScriptedModel([rubbish, rubbish, rubbish]))

        description = apps.linear.issues[state.linear_issue_id or ""]["description"]
        assert "Captured traceback" in description
        assert "Failing test" not in description

    async def test_a_red_suite_on_base_stops_retrying_immediately(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        # Nothing about a broken checkout improves by asking the model again.
        sandbox = ScriptedSandbox(pytest_exit_codes=[1], failure_output="2 failed")
        model = ScriptedModel()

        state = await run(Apps(guard, sandbox), ledger, repo, model)

        assert state.outcome is Outcome.CONFIRMED_NO_TEST
        assert state.reason is Reason.SUITE_RED_ON_BASE
        assert model.calls.count("SynthesizedTest") == 1

    async def test_the_gate_result_is_recorded_in_the_ledger(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        apps = Apps(guard, ScriptedSandbox())

        state = await run(apps, ledger, repo, ScriptedModel())

        gate_events = [e for e in ledger.events(state.run_id) if e["kind"] == "gate_result"]
        assert len(gate_events) == 1
        assert gate_events[0]["step"] == Step.GATE.value


class TestWorktreeIsolation:
    """Nothing a run does touches the real checkout."""

    async def test_the_repository_is_left_untouched(
        self, guard: Any, ledger: Ledger, repo: Path
    ) -> None:
        before = sorted(path.name for path in repo.rglob("*") if path.is_file())

        await run(Apps(guard, ScriptedSandbox()), ledger, repo, ScriptedModel())

        assert sorted(path.name for path in repo.rglob("*") if path.is_file()) == before
