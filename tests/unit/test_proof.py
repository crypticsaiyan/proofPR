"""The proof checks, and the block they render."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from proofpr.proof import checks
from proofpr.steps import worktree as wt
from tests.fixtures.sandboxes import ScriptedSandbox

BEFORE = """\
def parse(month, day):
    length = LENGTHS.get(month)
    if day > length:
        raise ValueError("day out of range")
    return month, day
"""

AFTER = """\
def parse(month, day):
    length = LENGTHS.get(month)
    if length is None:
        raise ValueError("month out of range")
    if day > length:
        raise ValueError("day out of range")
    return month, day
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A worktree with the patched file and a test in it."""
    wt.write_file(tmp_path, "src/mod.py", AFTER)
    wt.write_file(tmp_path, "tests/test_repro.py", "def test_x():\n    assert True\n")
    return tmp_path


async def run_checks(tree: Path, sandbox: Any) -> checks.ProofReport:
    """Run the proof checks against a scripted sandbox."""
    return await checks.run(
        sandbox=sandbox,
        worktree_path=tree,
        test_path="tests/test_repro.py",
        patched_files={"src/mod.py": AFTER},
        originals={"src/mod.py": BEFORE},
        report=checks.ProofReport(
            test_failed_on_base=True,
            base_failure="TypeError",
            test_passes_with_patch=True,
            suite_passes_with_patch=True,
        ),
    )


class TestRevertCheck:
    """Putting the bug back must break the test again."""

    async def test_a_patch_whose_revert_still_passes_is_not_proof(self, tree: Path) -> None:
        # Revert run passes, so the test was green for some other reason.
        report = await run_checks(tree, ScriptedSandbox(pytest_exit_codes=[0]))

        assert report.revert_reintroduces is False
        assert report.complete is False
        assert "revert" in (report.failure or "")

    async def test_the_patch_is_restored_after_the_revert_check(self, tree: Path) -> None:
        await run_checks(tree, ScriptedSandbox(pytest_exit_codes=[0]))

        assert (tree / "src" / "mod.py").read_text() == AFTER

    async def test_a_failing_revert_run_continues_to_the_later_checks(self, tree: Path) -> None:
        report = await run_checks(tree, ScriptedSandbox(pytest_exit_codes=[1], default_exit_code=1))

        assert report.revert_reintroduces is True
        assert report.flake_runs_passed == 0


class TestFlakeCheck:
    """Three consecutive passes, not three attempts."""

    async def test_three_passes_are_required(self, tree: Path) -> None:
        report = await run_checks(
            tree, ScriptedSandbox(pytest_exit_codes=[1, 0, 0, 0], default_exit_code=1)
        )

        assert report.flake_runs_passed == checks.FLAKE_RUNS

    async def test_one_failure_stops_the_check(self, tree: Path) -> None:
        report = await run_checks(
            tree, ScriptedSandbox(pytest_exit_codes=[1, 0, 1], default_exit_code=0)
        )

        assert report.flake_runs_passed == 1
        assert report.complete is False
        assert "1 of 3 runs" in (report.failure or "")


class TestMutationCheck:
    """Every mutant on the patched lines must be caught."""

    async def test_all_mutants_killed_completes_the_proof(self, tree: Path) -> None:
        # Revert fails, three flake runs pass, every mutant run fails: killed.
        sandbox = ScriptedSandbox(pytest_exit_codes=[1, 0, 0, 0], default_exit_code=1)

        report = await run_checks(tree, sandbox)

        assert report.mutants
        assert report.mutants_killed == len(report.mutants)
        assert report.survivors == []
        assert report.complete is True

    async def test_a_surviving_mutant_blocks_publication(self, tree: Path) -> None:
        # The first mutant run passes, which means the test did not notice it.
        sandbox = ScriptedSandbox(pytest_exit_codes=[1, 0, 0, 0, 0], default_exit_code=1)

        report = await run_checks(tree, sandbox)

        assert report.survivors
        assert report.complete is False
        assert "survived" in (report.failure or "")

    async def test_the_patched_file_is_restored_after_mutation(self, tree: Path) -> None:
        sandbox = ScriptedSandbox(pytest_exit_codes=[1, 0, 0, 0], default_exit_code=1)

        await run_checks(tree, sandbox)

        assert (tree / "src" / "mod.py").read_text() == AFTER

    async def test_a_patch_with_no_mutants_is_not_proof(self, tree: Path) -> None:
        # Nothing changed, so nothing can be mutated, so nothing is proved.
        report = await checks.run(
            sandbox=ScriptedSandbox(pytest_exit_codes=[1, 0, 0, 0]),
            worktree_path=tree,
            test_path="tests/test_repro.py",
            patched_files={"src/mod.py": AFTER},
            originals={"src/mod.py": AFTER},
            report=checks.ProofReport(
                test_failed_on_base=True, test_passes_with_patch=True, suite_passes_with_patch=True
            ),
        )

        assert report.mutants == []
        assert report.complete is False
        assert "no mutants" in (report.failure or "")


class TestProofBlock:
    """Every row is a command a reviewer can rerun."""

    def block(self, **overrides: Any) -> str:
        """Render a proof block from a complete report."""
        report = checks.ProofReport(
            test_failed_on_base=True,
            base_failure="TypeError",
            test_passes_with_patch=True,
            suite_passes_with_patch=True,
            suite_summary="142 passed in 2.1s",
            revert_reintroduces=True,
            flake_runs_passed=3,
            mutants=[
                checks.MutantOutcome(
                    label="line 3: Is becomes IsNot", operator="comparison", killed=True
                ),
                checks.MutantOutcome(
                    label="line 4: the raise is removed", operator="raise_removal", killed=True
                ),
            ],
        )
        for key, value in overrides.items():
            setattr(report, key, value)
        return checks.render_block(
            report, test_path="tests/test_repro.py", exception="TypeError", run_id="r-7f3a"
        )

    def test_every_check_appears(self) -> None:
        block = self.block()

        assert "New test on base commit" in block
        assert "Revert patch, rerun new test" in block
        assert "Mutants on patched lines" in block
        assert "2/2 killed" in block
        assert "PASSED 3/3 runs" in block

    def test_the_run_marker_is_present_for_idempotent_retry(self) -> None:
        assert "proofpr-run:r-7f3a" in self.block()

    def test_survivors_are_named_rather_than_hidden(self) -> None:
        block = self.block(
            mutants=[
                checks.MutantOutcome(
                    label="line 3: Is becomes IsNot", operator="comparison", killed=False
                )
            ]
        )

        assert "Surviving mutants" in block
        assert "line 3: Is becomes IsNot" in block
        assert "0/1 killed" in block

    def test_a_failed_revert_is_stated_plainly(self) -> None:
        block = self.block(revert_reintroduces=False)

        assert "PASSED (the test does not detect the bug)" in block
