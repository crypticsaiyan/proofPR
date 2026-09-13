"""One report, all the way to a pull request, against real containers.

The only things faked here are the four applications, because writing to real
ones needs credentials. The sandbox, the patch, the proof checks, the mutants,
and the guard are all real, and the model is scripted so the test measures the
pipeline rather than a model's mood.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.adapters.docker_sandbox import DockerSandbox, SandboxLimits
from proofpr.adapters.memory import FakeDiscord, FakeGitHub, FakeLinear
from proofpr.domain.enums import Outcome
from proofpr.domain.run import Report
from proofpr.ledger import Ledger
from proofpr.pipeline import Config, Pipeline
from proofpr.steps.approve import AutoApprover
from proofpr.steps.patch import PatchedFile, ProposedPatch
from proofpr.steps.test_synth import SynthesizedTest
from proofpr.triage.intent import IntentVerdict
from tests.conftest import REPO_ROOT
from tests.integration.test_sandbox_reproduction import IMAGE, image_present

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.skipif(not image_present(), reason="build it with `just sandbox-image`"),
]

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

TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
"""


def fixed_source() -> str:
    """The sample file with the bug actually fixed."""
    original = (VALIDKIT / "src" / "validkit" / "dates.py").read_text()
    return original.replace(
        "    if day > length:",
        '    if length is None:\n        raise ValueError(f"month {month} is out of range")\n'
        "    if day > length:",
    )


class ScriptedModel:
    """Returns the test and the patch a good model would return."""

    async def complete_json(
        self, *, system: str, user: str, schema: type[Any], tier: str = "cheap", **kwargs: Any
    ) -> tuple[Any, Any]:
        """Return the scripted object for the requested schema."""
        usage = type(
            "Usage",
            (),
            {
                "tier": tier,
                "model": "scripted",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": 0.02,
            },
        )()
        if schema is IntentVerdict:
            return IntentVerdict(intent="bug", confidence=0.98), usage
        if schema is SynthesizedTest:
            return SynthesizedTest(path="tests/test_proofpr_repro.py", code=TEST), usage
        if schema is ProposedPatch:
            return (
                ProposedPatch(
                    files=[PatchedFile(path="src/validkit/dates.py", content=fixed_source())],
                    summary="validate the month before looking up its length",
                    rationale="An invalid month yields None, and the comparison then raises.",
                ),
                usage,
            )
        raise AssertionError(f"unexpected schema: {schema}")


class Apps:
    """In-memory applications with a real sandbox."""

    def __init__(self, guard: Any) -> None:
        """Build the bundle."""
        self.discord = FakeDiscord(guard)
        self.github = FakeGitHub(guard)
        self.linear = FakeLinear(guard)
        self.sandbox = DockerSandbox(SandboxLimits(image=IMAGE, timeout_seconds=90))
        self.guard = guard


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the sample repository."""
    destination = tmp_path / "validkit"
    shutil.copytree(VALIDKIT, destination)
    return destination


async def test_a_report_becomes_a_proof_carrying_pull_request(
    guard: Any, tmp_path: Path, repo: Path
) -> None:
    apps = Apps(guard)
    ledger = Ledger(tmp_path / "ledger.db")
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

    # The whole claim, end to end.
    assert state.outcome is Outcome.PR_OPENED, state.reason
    assert state.repro_outcome == "confirmed"
    assert state.gate_passed is True
    assert state.proof_complete is True
    assert state.mutants_total > 0
    assert state.mutants_killed == state.mutants_total
    assert state.pr_ready is True
    assert state.consistent is True

    body = apps.github.pulls[str(state.pr_number)]["body"]
    assert "Revert patch, rerun new test | FAILED" in body
    assert f"{state.mutants_killed}/{state.mutants_total} killed" in body

    assert ledger.verify_chain(state.run_id) == (True, None)
    assert ledger.total_cost(state.run_id) > 0


async def test_a_patch_that_does_not_fix_anything_never_reaches_a_pull_request(
    guard: Any, tmp_path: Path, repo: Path
) -> None:
    class UselessModel(ScriptedModel):
        """Proposes a patch that changes nothing that matters."""

        async def complete_json(self, *, schema: type[Any], **kwargs: Any) -> tuple[Any, Any]:
            """Return a no-op patch when asked for a fix."""
            if schema is ProposedPatch:
                original = (VALIDKIT / "src" / "validkit" / "dates.py").read_text()
                usage = type(
                    "Usage",
                    (),
                    {
                        "tier": "strong",
                        "model": "scripted",
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cost_usd": 0.01,
                    },
                )()
                return (
                    ProposedPatch(
                        files=[
                            PatchedFile(
                                path="src/validkit/dates.py",
                                content=original.replace(
                                    '"""Date parsing."""', '"""Date parsing module."""'
                                ),
                            )
                        ],
                        summary="tidy the docstring",
                    ),
                    usage,
                )
            return await super().complete_json(schema=schema, **kwargs)

    apps = Apps(guard)
    ledger = Ledger(tmp_path / "ledger.db")
    pipeline = Pipeline(
        apps=apps,
        ledger=ledger,
        model=UselessModel(),
        approver=AutoApprover(),
        config=Config(
            supported_versions=">=0.3.0",
            patch_paths=("src/**",),
            repo_path=repo,
            package="validkit",
        ),
    )

    state = await pipeline.run(
        Report(source="discord", text=REPORT, author="mira", channel_id=1, message_id=2)
    )

    assert state.outcome is Outcome.ABANDONED
    assert apps.github.pulls == {}
    assert apps.linear.issues, "the work is still filed for a human"
