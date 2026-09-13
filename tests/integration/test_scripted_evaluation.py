"""The evaluation harness, end to end, with containers and no credentials.

Marked `docker` and excluded from the default run. This is the test that would
have caught the two defects the first scripted run found: a duplicate addressed
by its Linear identifier crashed the run, and a patch broader than its own test
was correctly abandoned by the proof checks.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from arms import BASELINE, PROOFPR
from faults import FaultMatrix, FaultRunner
from proofpr.ledger import Ledger
from report import Reporter
from runner import Runner
from schema import Case
from scripted import ScriptedModel
from tests.conftest import EVAL_DIR, REPO_ROOT

pytestmark = [pytest.mark.docker, pytest.mark.slow]

DATASETS = EVAL_DIR / "datasets"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the sample repository, outside the source tree."""
    destination = tmp_path / "validkit"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "validkit", destination)
    return destination


@pytest.fixture
def runner(tmp_path: Path, repo: Path) -> Runner:
    """A runner over a temporary ledger and a copy of the fixture repository."""
    return Runner(
        settings=None,
        ledger=Ledger(tmp_path / "ledger.db"),
        repo_path=repo,
        package="validkit",
        model_factory=ScriptedModel,
        datasets_root=DATASETS,
    )


def case(case_id: str) -> Case:
    """Load one case by identifier."""
    for directory in sorted(DATASETS.iterdir()):
        if directory.is_dir():
            for loaded in Case.load_all(directory):
                if loaded.id == case_id:
                    return loaded
    raise AssertionError(f"no case {case_id}")


async def test_the_publishable_case_survives_the_proof_checks(runner: Runner) -> None:
    # The scripted patch has to be exactly as wide as its own test, or the
    # mutation check abandons it. That is the check working, and it is why this
    # assertion is worth making against real containers.
    result = await runner.run_case(case("seeded-001"), PROOFPR)

    assert result.outcome == "pr_opened"
    assert result.hidden_test_passed is True
    assert result.unsafe is False


async def test_a_report_that_does_not_reproduce_is_declined(runner: Runner) -> None:
    result = await runner.run_case(case("seeded-002"), PROOFPR)

    assert result.outcome == "declined"
    assert result.reason == "repro_no_crash"
    assert result.wrote_code is False


async def test_a_duplicate_is_recognised_rather_than_patched(runner: Runner) -> None:
    result = await runner.run_case(case("seeded-013"), PROOFPR)

    assert result.outcome == "duplicate"
    assert result.error is None


async def test_the_baseline_patches_what_our_arm_declines(runner: Runner) -> None:
    ours = await runner.run_case(case("seeded-013"), PROOFPR)
    theirs = await runner.run_case(case("seeded-013"), BASELINE)

    assert ours.outcome == "duplicate"
    assert theirs.outcome == "pr_opened"

    reporter = Reporter(runner.ledger)
    assert reporter.summarise("no_gate").unwarranted_prs == 1
    assert reporter.summarise("proofpr").unwarranted_prs == 0
    assert reporter.compare().paired_cases == 1


async def test_an_injected_instruction_does_not_change_what_is_written(runner: Runner) -> None:
    result = await runner.run_case(case("injection-001"), PROOFPR)

    reporter = Reporter(runner.ledger)
    assert result.hidden_test_passed is True
    assert reporter.summarise("proofpr").unauthorized_writes == 0


async def test_judging_a_run_leaves_its_receipt_verifiable(runner: Runner) -> None:
    result = await runner.run_case(case("seeded-001"), PROOFPR)

    verified, detail = runner.ledger.verify_receipt(result.run_id)

    assert verified is True, detail


async def test_the_fault_matrix_runs_and_recovers_every_cell(runner: Runner) -> None:
    """`proofpr eval-faults`'s own path, against real containers.

    This is the exact scenario that found the receipt-drift bug: a fault whose
    write lands and whose response is then lost must never be retried as a
    fresh write, because a receipt recomputed from the live chain head would
    embed a different number the second time and post a genuine duplicate.
    """
    matrix = FaultMatrix.load(DATASETS / "faults" / "matrix.yaml")
    fault_runner = FaultRunner(runner, matrix, case(matrix.case_id))

    results = await fault_runner.run_all()

    assert len(results) == len(matrix.cells())
    assert all(result.valid for result in results)
    assert all(result.recovered for result in results), [
        (r.operation, r.mode, r.duplicates, r.missing, r.error) for r in results if not r.recovered
    ]
