"""The three checks that turn a passing test into evidence.

Section 4.6 of AGENTS.md. A patch that makes a test go from red to green has
demonstrated correlation. These checks are what make the pull request's claims
checkable by a reviewer who trusts nothing:

- **Revert.** Put the bug back. The test must fail again. If it does not, the
  test was passing for some other reason and the patch is incidental.
- **Flake.** Run the new test three times. All three must pass. A test that
  passes sometimes is not proof of anything, and it will waste the maintainer's
  time forever.
- **Mutation.** Alter the patched lines in small plausible ways. Every mutant
  must be caught by the new test. A surviving mutant means the test does not
  actually check the fix.

Each check is run in the same worktree, in this order, cheapest first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from proofpr.observability.logging import get_logger
from proofpr.proof import mutator
from proofpr.steps import worktree as wt

logger = get_logger("proofpr.proof")

FLAKE_RUNS = 3


@dataclass(frozen=True, slots=True)
class MutantOutcome:
    """One mutant and whether the test caught it."""

    label: str
    operator: str
    killed: bool


@dataclass
class ProofReport:
    """Everything the pull request's proof block asserts."""

    test_failed_on_base: bool = False
    base_failure: str = ""
    test_passes_with_patch: bool = False
    suite_passes_with_patch: bool = False
    suite_summary: str = ""
    revert_reintroduces: bool = False
    flake_runs_passed: int = 0
    mutants: list[MutantOutcome] = field(default_factory=list)

    @property
    def mutants_killed(self) -> int:
        """How many mutants the test caught."""
        return sum(1 for mutant in self.mutants if mutant.killed)

    @property
    def survivors(self) -> list[MutantOutcome]:
        """Mutants the test failed to catch."""
        return [mutant for mutant in self.mutants if not mutant.killed]

    @property
    def complete(self) -> bool:
        """Whether every check passed.

        A patch that fails any of these is not published. Mutation is included:
        a test that cannot tell the fix from its own negation is not proof, and
        publishing it under a proof block would be a lie.
        """
        return (
            self.test_failed_on_base
            and self.test_passes_with_patch
            and self.suite_passes_with_patch
            and self.revert_reintroduces
            and self.flake_runs_passed == FLAKE_RUNS
            and bool(self.mutants)
            and not self.survivors
        )

    @property
    def failure(self) -> str | None:
        """Why the proof is incomplete, in one line."""
        if not self.revert_reintroduces:
            return "reverting the patch did not make the test fail again"
        if self.flake_runs_passed != FLAKE_RUNS:
            return f"the test passed only {self.flake_runs_passed} of {FLAKE_RUNS} runs"
        if not self.mutants:
            return "no mutants could be generated for the patched lines"
        if self.survivors:
            surviving = ", ".join(mutant.label for mutant in self.survivors[:3])
            return f"{len(self.survivors)} mutants survived the new test: {surviving}"
        return None


async def run(
    *,
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    worktree_path: Path,
    test_path: str,
    patched_files: dict[str, str],
    originals: dict[str, str | None],
    report: ProofReport,
    timeout_seconds: int | None = None,
) -> ProofReport:
    """Run the revert, flake, and mutation checks against an applied patch.

    Args:
        sandbox: The sandbox to run in.
        worktree_path: A worktree with the patch and the test applied.
        test_path: The reproduction test.
        patched_files: Path to patched contents, as published.
        originals: Path to the contents before the patch, for the revert check.
        report: The report so far, carrying the red and green results.
        timeout_seconds: Overrides the sandbox timeout.

    Returns:
        The completed report.
    """
    # 1. Revert. Cheapest, and the check most likely to catch an incidental fix.
    for path, original in originals.items():
        wt.revert_file(worktree_path, path, original)
    reverted = await sandbox.run_pytest(
        worktree=str(worktree_path), target=test_path, timeout_seconds=timeout_seconds
    )
    report.revert_reintroduces = reverted["exit_code"] != 0
    for path, content in patched_files.items():
        wt.write_file(worktree_path, path, content)
    if not report.revert_reintroduces:
        logger.info("revert_check_failed")
        return report

    # 2. Flake. Three consecutive passes, not three attempts.
    for _ in range(FLAKE_RUNS):
        run_result = await sandbox.run_pytest(
            worktree=str(worktree_path), target=test_path, timeout_seconds=timeout_seconds
        )
        if run_result["exit_code"] != 0:
            logger.info("flake_check_failed", passed=report.flake_runs_passed)
            return report
        report.flake_runs_passed += 1

    # 3. Mutation, scoped to the lines this patch changed.
    for path, content in patched_files.items():
        original = originals.get(path) or ""
        lines = mutator.changed_lines(original, content)
        for mutant in mutator.generate(content, lines):
            wt.write_file(worktree_path, path, mutant.code)
            mutant_run = await sandbox.run_pytest(
                worktree=str(worktree_path), target=test_path, timeout_seconds=timeout_seconds
            )
            report.mutants.append(
                MutantOutcome(
                    label=mutant.label,
                    operator=mutant.operator,
                    killed=mutant_run["exit_code"] != 0,
                )
            )
        wt.write_file(worktree_path, path, content)

    logger.info(
        "proof_complete",
        complete=report.complete,
        mutants=len(report.mutants),
        killed=report.mutants_killed,
    )
    return report


def render_block(
    report: ProofReport,
    *,
    test_path: str,
    exception: str | None,
    links: dict[str, str] | None = None,
    receipt: str | None = None,
    run_id: str | None = None,
) -> str:
    """Render the proof table that goes in the pull request body.

    Every row is a command a reviewer can rerun. That is the whole point: the
    block is not a summary of what the agent believes, it is a list of checks
    with their results.
    """
    expected = exception or "the reported failure"
    rows = [
        ("New test on base commit", f"FAILED: {report.base_failure or expected}"),
        (
            "New test with patch",
            f"PASSED {report.flake_runs_passed}/{FLAKE_RUNS} runs"
            if report.flake_runs_passed
            else "PASSED",
        ),
        ("Full suite with patch", report.suite_summary or "passed"),
        (
            "Revert patch, rerun new test",
            "FAILED (the bug is detected again)"
            if report.revert_reintroduces
            else "PASSED (the test does not detect the bug)",
        ),
        (
            "Mutants on patched lines",
            f"{report.mutants_killed}/{len(report.mutants)} killed"
            if report.mutants
            else "none generated",
        ),
    ]

    lines = ["## Proof", "", "| Check | Result |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in rows]

    if report.survivors:
        lines += ["", "Surviving mutants:"]
        lines += [f"- {mutant.label}" for mutant in report.survivors]

    lines += ["", f"Test: `{test_path}`"]
    if links:
        lines.append("Inputs: " + " · ".join(f"[{name}]({url})" for name, url in links.items()))
    if receipt:
        lines.append(f"Receipt: `sha256:{receipt[:16]}`")
    if run_id:
        lines += ["", f"<!-- proofpr-run:{run_id} -->"]
    return "\n".join(lines)
