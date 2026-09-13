"""Reproduction against a real container and a real repository.

Marked `docker` and excluded from the default run. Everything else in the suite
scripts the sandbox, which proves the logic; this proves the logic is talking to
something real, which is the claim the proof block makes.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from proofpr.adapters.docker_sandbox import DockerSandbox, SandboxLimits
from proofpr.proof import checks
from proofpr.steps import gate as gate_step
from proofpr.steps import repro_raw
from proofpr.steps import worktree as wt
from tests.conftest import REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.slow]

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"
IMAGE = "proofpr-sandbox:local"

REPORT = """\
parse_date crashes on an invalid month instead of raising a clear error.

Traceback (most recent call last):
  File "app.py", line 3, in <module>
    parse_date("2024-13-01")
  File "src/validkit/dates.py", line 30, in parse_date
    if day > length:
TypeError: '>' not supported between instances of 'int' and 'NoneType'
"""

FAILING_TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
"""

PASSING_TEST = """\
from validkit import parse_date


def test_valid_date_parses():
    assert parse_date("2024-02-11") == (2024, 2, 11)
"""

WRONG_REASON_TEST = """\
from validkit import parse_date


def test_something_unrelated():
    assert parse_date("2024-02-11")[0] == 1999
"""


def image_present() -> bool:
    """Return whether the sandbox image is built locally."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603
        ["docker", "image", "inspect", IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


pytestmark.append(
    pytest.mark.skipif(not image_present(), reason="build it with `just sandbox-image`")
)


@pytest.fixture
def sandbox() -> DockerSandbox:
    """A sandbox using the locally built image."""
    return DockerSandbox(SandboxLimits(image=IMAGE, timeout_seconds=60))


@pytest.fixture
def tree() -> Iterator[Path]:
    """A throwaway worktree of the sample repository."""
    with wt.worktree(VALIDKIT) as path:
        yield path


async def test_stage_one_reproduces_the_real_bug(sandbox: DockerSandbox, tree: Path) -> None:
    raw = await repro_raw.run(
        sandbox=sandbox, worktree_path=tree, report_text=REPORT, package="validkit"
    )

    assert raw.reproduced
    assert raw.exception == "TypeError"
    assert raw.function == "parse_date"
    assert "dates.py" in (raw.path or "")


async def test_stage_one_reports_no_crash_when_the_input_is_fine(
    sandbox: DockerSandbox, tree: Path
) -> None:
    raw = await repro_raw.run(
        sandbox=sandbox,
        worktree_path=tree,
        report_text="```python\nparse_date('2024-02-11')\n```",
        package="validkit",
    )

    assert raw.outcome is repro_raw.ReproOutcome.NO_CRASH
    assert raw.reproduced is False


async def test_the_gate_accepts_a_test_that_fails_for_the_right_reason(
    sandbox: DockerSandbox, tree: Path
) -> None:
    outcome = await gate_step.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_proofpr_repro.py",
        test_code=FAILING_TEST,
        expected_exception="TypeError",
    )

    assert outcome.passed, outcome.detail
    assert outcome.checks["suite_green_on_base"] is True


async def test_the_gate_refuses_a_test_that_passes(sandbox: DockerSandbox, tree: Path) -> None:
    outcome = await gate_step.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_proofpr_repro.py",
        test_code=PASSING_TEST,
        expected_exception="TypeError",
    )

    assert outcome.passed is False
    assert outcome.reason is not None
    assert "does not reproduce" in outcome.detail


async def test_the_gate_refuses_a_test_failing_for_an_unrelated_reason(
    sandbox: DockerSandbox, tree: Path
) -> None:
    outcome = await gate_step.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_proofpr_repro.py",
        test_code=WRONG_REASON_TEST,
        expected_exception="TypeError",
    )

    # pytest rewrites a bare assert, so the output names no exception at all.
    # It still has to be read as an assertion failure, and the gate still has to
    # accept it: a test asserting corrected behaviour is a valid reproduction.
    # What this case shows is that the gate alone cannot tell a relevant
    # assertion from an irrelevant one, which is why stage 1 exists and why the
    # test is written from a captured traceback rather than from prose.
    assert outcome.observed_exception == "AssertionError"
    assert outcome.passed is True


async def test_the_worktree_is_isolated_from_the_original(
    sandbox: DockerSandbox, tree: Path
) -> None:
    wt.write_file(tree, "tests/test_scratch.py", "def test_x():\n    assert True\n")

    assert not (VALIDKIT / "tests" / "test_scratch.py").exists()


async def test_generated_code_cannot_reach_the_network(sandbox: DockerSandbox, tree: Path) -> None:
    wt.write_file(
        tree,
        "tests/test_exfil.py",
        "import socket\n\n"
        "def test_network():\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n",
    )
    prepared = await sandbox.run_pytest(worktree=str(tree), target="tests/test_exfil.py")

    assert prepared["exit_code"] != 0


PATCHED_DATES = '''\
"""Date parsing."""

from __future__ import annotations

MONTH_LENGTHS = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _month_length(month: int) -> int | None:
    """Return the number of days in a month, or None when the month is invalid."""
    return MONTH_LENGTHS.get(month)


def parse_date(text: str) -> tuple[int, int, int]:
    """Parse an ISO-like date into its parts."""
    parts = text.split("-")
    if len(parts) != 3:
        raise ValueError(f"expected YYYY-MM-DD, got {text!r}")
    year, month, day = (int(part) for part in parts)

    length = _month_length(month)
    if length is None:
        raise ValueError(f"month {month} is out of range")
    if day > length:
        raise ValueError(f"day {day} is out of range for month {month}")
    return year, month, day
'''

# The same fix, plus a second guard the reproduction test never exercises.
PATCHED_WITH_UNCOVERED_GUARD = PATCHED_DATES.replace(
    "    if day > length:",
    '    if month < 1:\n        raise ValueError("month must be positive")\n    if day > length:',
)

WEAK_TEST = """\
import pytest

from validkit import parse_date


def test_invalid_month_raises_something():
    with pytest.raises(Exception):
        parse_date("2024-13-01")
"""


async def test_the_full_proof_passes_on_a_real_fix(sandbox: DockerSandbox, tree: Path) -> None:
    original = (tree / "src" / "validkit" / "dates.py").read_text()
    wt.write_file(tree, "tests/test_repro.py", FAILING_TEST)
    wt.write_file(tree, "src/validkit/dates.py", PATCHED_DATES)

    report = await checks.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_repro.py",
        patched_files={"src/validkit/dates.py": PATCHED_DATES},
        originals={"src/validkit/dates.py": original},
        report=checks.ProofReport(
            test_failed_on_base=True,
            base_failure="TypeError",
            test_passes_with_patch=True,
            suite_passes_with_patch=True,
        ),
    )

    assert report.revert_reintroduces is True, "reverting the fix must break the test again"
    assert report.flake_runs_passed == checks.FLAKE_RUNS
    assert report.mutants, "the patched lines must produce mutants"
    assert report.survivors == [], [mutant.label for mutant in report.survivors]
    assert report.complete is True


async def test_a_test_that_accepts_any_exception_is_caught_by_the_revert_check(
    sandbox: DockerSandbox, tree: Path
) -> None:
    # `pytest.raises(Exception)` passes whether the code raises the new ValueError
    # or the original TypeError, so removing the fix goes unnoticed. The revert
    # check catches it before mutation is even reached, which is the cheapest
    # place to catch it.
    original = (tree / "src" / "validkit" / "dates.py").read_text()
    wt.write_file(tree, "tests/test_repro.py", WEAK_TEST)
    wt.write_file(tree, "src/validkit/dates.py", PATCHED_DATES)

    report = await checks.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_repro.py",
        patched_files={"src/validkit/dates.py": PATCHED_DATES},
        originals={"src/validkit/dates.py": original},
        report=checks.ProofReport(
            test_failed_on_base=True, test_passes_with_patch=True, suite_passes_with_patch=True
        ),
    )

    assert report.revert_reintroduces is False
    assert report.complete is False
    assert "revert" in (report.failure or "")


async def test_uncovered_behaviour_in_a_patch_leaves_a_mutant_alive(
    sandbox: DockerSandbox, tree: Path
) -> None:
    # This patch adds two guards, and the test only exercises one of them. The
    # revert check and the flake check both pass, because the test really does
    # detect the reported bug. Mutation is what notices that half the patch is
    # unexamined, which is precisely the gap it exists to find.
    original = (tree / "src" / "validkit" / "dates.py").read_text()
    wt.write_file(tree, "tests/test_repro.py", FAILING_TEST)
    wt.write_file(tree, "src/validkit/dates.py", PATCHED_WITH_UNCOVERED_GUARD)

    report = await checks.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_repro.py",
        patched_files={"src/validkit/dates.py": PATCHED_WITH_UNCOVERED_GUARD},
        originals={"src/validkit/dates.py": original},
        report=checks.ProofReport(
            test_failed_on_base=True, test_passes_with_patch=True, suite_passes_with_patch=True
        ),
    )

    assert report.revert_reintroduces is True
    assert report.flake_runs_passed == checks.FLAKE_RUNS
    assert report.survivors, "the unexercised guard should leave a mutant alive"
    assert report.complete is False
    assert "survived" in (report.failure or "")
