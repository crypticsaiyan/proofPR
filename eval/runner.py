"""The evaluation runner.

Runs cases through the real pipeline against in-memory applications, records
everything in a ledger, and stops there. It computes nothing: every number comes
from `report.py` reading the ledger afterwards, so a runner bug cannot quietly
improve a result.

Two things make the output a measurement rather than a demonstration:

- Both arms run over identical case identifiers in the same session, so the
  comparison is paired and case mix cannot explain a difference.
- The hidden test for a case is applied **after** the run, against whatever patch
  the arm produced. It is never mounted into a worktree the agent can read.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arms import ARMS, ArmSpec
from proofpr.adapters.docker_sandbox import DockerSandbox, SandboxLimits
from proofpr.adapters.memory import FakeDiscord, FakeGitHub, FakeLinear
from proofpr.domain.errors import ConfigurationError
from proofpr.domain.run import Report
from proofpr.guard import Guard
from proofpr.ledger import Ledger
from proofpr.observability.logging import get_logger
from proofpr.pipeline import Config, Pipeline
from proofpr.settings import Settings
from proofpr.steps import worktree as wt
from proofpr.steps.approve import AutoApprover
from schema import Case

logger = get_logger("proofpr.eval")

#: Where a case's hidden test lives, inside its own dataset directory.
#: Committed for seeded cases, which we wrote, and absent for real bugs, whose
#: hidden tests come from upstream and are never committed. Neither is ever
#: mounted into a worktree the agent reads.
HIDDEN_DIR_NAME = "hidden"

#: Where real_bugs checkouts are fetched to, one per case id, pinned to that
#: case's `buggy_commit`. Never committed: see eval/fetch_real_bugs.py.
REAL_BUGS_REPOS_DEFAULT = Path("eval/real_bugs_repos")


@dataclass(frozen=True, slots=True)
class CaseTarget:
    """The repository, package, and sandbox one case actually runs against.

    Most cases (seeded, injection, faults) share the single repo/package a
    `Runner` was built with. A `real_bugs` case names its own real repository,
    pinned to its own buggy commit, under its own package directory, and needs
    its own sandbox image: the sandbox has no network at run time, so each
    library's dependencies have to already be baked into an image built ahead
    of time (see sandbox/real_bugs/<case-id>/Dockerfile).
    """

    repo_path: Path
    package: str
    patch_paths: tuple[str, ...]
    supported_versions: str
    sandbox_image: str
    hidden_test_path: str


@dataclass
class CaseResult:
    """One case, in one arm."""

    case_id: str
    arm: str
    run_id: str
    outcome: str
    reason: str | None
    expected_outcome: str
    cost_usd: float
    wrote_code: bool
    hidden_test_passed: bool | None = None
    hidden_test_detail: str = ""
    error: str | None = None

    @property
    def outcome_correct(self) -> bool:
        """Whether the terminal state matched the ground truth."""
        return self.outcome == self.expected_outcome

    @property
    def unsafe(self) -> bool:
        """Whether this arm shipped a patch that the maintainer's test rejects.

        A run that wrote no code cannot be unsafe. A run that wrote code and has
        no hidden test to judge it by is not counted either way, because counting
        an unjudged patch as safe is exactly the flattery this metric exists to
        avoid.
        """
        return self.wrote_code and self.hidden_test_passed is False


@dataclass
class RunnerReport:
    """Everything one invocation produced."""

    split: str
    results: list[CaseResult] = field(default_factory=list)
    ledger_path: Path | None = None

    def by_arm(self, arm: str) -> list[CaseResult]:
        """Return the results for one arm."""
        return [result for result in self.results if result.arm == arm]


class MemoryApps:
    """In-memory applications with a real sandbox.

    The three applications are faked because writing to real ones needs
    credentials and would make the evaluation destructive. Everything that
    decides an outcome is real: the sandbox, the guard, the ledger, the rules.
    """

    def __init__(self, guard: Guard, sandbox: Any) -> None:  # noqa: ANN401
        """Build the bundle."""
        self.discord = FakeDiscord(guard)
        self.github = FakeGitHub(guard)
        self.linear = FakeLinear(guard)
        self.sandbox = sandbox
        self.guard = guard


class Runner:
    """Runs cases through both arms and records the results."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: Ledger,
        repo_path: Path,
        package: str,
        model_factory: Any,  # noqa: ANN401 - takes a Case, returns a ModelPort
        sandbox_image: str = "proofpr-sandbox:local",
        datasets_root: Path | None = None,
        real_bugs_root: Path | None = None,
    ) -> None:
        """Build the runner."""
        self.settings = settings
        self.ledger = ledger
        self.repo_path = repo_path
        self.package = package
        self.model_factory = model_factory
        self.sandbox_image = sandbox_image
        self.datasets_root = datasets_root
        self.real_bugs_root = real_bugs_root or REAL_BUGS_REPOS_DEFAULT

    def target_for(self, case: Case) -> CaseTarget:
        """Resolve which repository, package, and sandbox a case runs against.

        A case with no `repo_url` shares this runner's one configured
        repo/package (validkit, for every seeded, injection, and fault case).
        A `real_bugs` case names its own, so it gets its own checkout path,
        patch scope, and sandbox image instead.
        """
        if case.repo_url is None:
            return CaseTarget(
                repo_path=self.repo_path,
                package=self.package,
                patch_paths=("src/**",),
                supported_versions=">=0.3.0",
                sandbox_image=self.sandbox_image,
                hidden_test_path=case.hidden_test_path,
            )
        if not case.package:
            raise ConfigurationError(f"case {case.id} sets repo_url but not package")
        return CaseTarget(
            repo_path=self.real_bugs_root / case.id,
            package=case.package,
            patch_paths=(f"{case.package}/**",),
            supported_versions=">=0.0.0",
            # One image per case, not per package: two cases on the same
            # library can be pinned to commits years apart, with different
            # dependency sets and even different Python versions (see
            # sandbox/real_bugs/<case-id>/Dockerfile).
            sandbox_image=f"proofpr-real-bugs-{case.id}:local",
            hidden_test_path=case.hidden_test_path,
        )

    def build_apps(self, case: Case) -> MemoryApps:
        """In-memory applications seeded for one case, with a real sandbox."""
        target = self.target_for(case)
        apps = MemoryApps(
            Guard.load(),
            DockerSandbox(SandboxLimits(image=target.sandbox_image, timeout_seconds=90)),
        )
        self._seed(apps, case)
        return apps

    def build_pipeline(self, apps: MemoryApps, case: Case, spec: ArmSpec) -> Pipeline:
        """The pipeline every evaluation run uses.

        Write retries do not sleep: the applications are in memory, and a delay
        would only make the evaluation slower without changing what it measures.
        """
        target = self.target_for(case)
        return Pipeline(
            apps=apps,
            ledger=self.ledger,
            model=self.model_factory(case),
            approver=AutoApprover(),
            arm=spec.arm,
            config=Config(
                supported_versions=target.supported_versions,
                patch_paths=target.patch_paths,
                repo_path=target.repo_path,
                package=target.package,
                ci_poll_seconds=0,
                ci_timeout_minutes=1,
                write_backoff_seconds=0.0,
            ),
        )

    async def run_case(self, case: Case, spec: ArmSpec) -> CaseResult:
        """Run one case in one arm, then judge it with the hidden test."""
        apps = self.build_apps(case)
        pipeline = self.build_pipeline(apps, case, spec)

        try:
            state = await pipeline.run(
                Report(
                    source=case.source,
                    text=case.text,
                    author=case.author,
                    channel_id=1,
                    message_id=1,
                )
            )
        except Exception as error:  # noqa: BLE001 - a crashed case is a result, not a stop
            logger.warning("case_crashed", case=case.id, arm=spec.label, error=str(error))
            return CaseResult(
                case_id=case.id,
                arm=spec.label,
                run_id="",
                outcome="crashed",
                reason=None,
                expected_outcome=case.expected_outcome,
                cost_usd=0.0,
                wrote_code=False,
                error=str(error),
            )

        wrote_code = state.patch_code is not None
        result = CaseResult(
            case_id=case.id,
            arm=spec.label,
            run_id=state.run_id,
            outcome=state.outcome.value if state.outcome else "none",
            reason=state.reason.value if state.reason else None,
            expected_outcome=case.expected_outcome,
            cost_usd=self.ledger.total_cost(state.run_id),
            wrote_code=wrote_code,
        )

        # Annotations, not events: the run is sealed and its receipt already
        # published. A verdict reached afterwards belongs beside the run, never
        # inside the chain it would invalidate.
        self.ledger.annotate(state.run_id, "case", case.id)
        self.ledger.annotate(state.run_id, "expected_outcome", case.expected_outcome)
        if wrote_code:
            passed, detail = await self._apply_hidden_test(
                case, state.patched_path, state.patch_code
            )
            result.hidden_test_passed = passed
            result.hidden_test_detail = detail
            self.ledger.annotate(
                state.run_id,
                "hidden_test",
                {"case": case.id, "passed": passed, "detail": detail[:500]},
            )
        return result

    async def run_split(self, cases: Sequence[Case], *, arms: Sequence[str]) -> RunnerReport:
        """Run every case in every arm, sequentially and paired."""
        report = RunnerReport(split="custom", ledger_path=self.ledger.path)
        for case in cases:
            for label in arms:
                spec = ARMS[label]
                logger.info("case_started", case=case.id, arm=spec.label)
                report.results.append(await self.run_case(case, spec))
        return report

    def _seed(self, apps: MemoryApps, case: Case) -> None:
        """Insert the issues a case says already exist."""
        for seeded in case.seed:
            if seeded.app == "linear":
                issue_id = str(len(apps.linear.issues) + 1000)
                apps.linear.issues[issue_id] = {
                    "id": issue_id,
                    "identifier": f"ENG-{issue_id}",
                    "title": seeded.title,
                    "description": seeded.body,
                    "url": f"fake://issue/{issue_id}",
                    "state": {"name": "Todo", "type": "unstarted"},
                    "comments": {"nodes": []},
                    "attachments": {"nodes": []},
                }
            elif seeded.app == "github":
                apps.github.search_results.append(
                    {
                        "number": 900 + len(apps.github.search_results),
                        "title": seeded.title,
                        "body": seeded.body,
                        "state": seeded.state,
                        "html_url": "https://github.com/acme/validkit/pull/900",
                        **({"pull_request": {"url": "x"}} if seeded.state == "open" else {}),
                    }
                )

    async def _apply_hidden_test(
        self, case: Case, patched_path: str | None, patch_code: str | None
    ) -> tuple[bool | None, str]:
        """Judge a patch with a test the agent never saw.

        Returns `(None, reason)` when there is no hidden test for the case, which
        is reported as unjudged rather than counted as a pass.
        """
        hidden = self._hidden_test(case)
        if hidden is None:
            return None, "no hidden test for this case"
        if patched_path is None or patch_code is None:
            return None, "no patch to judge"

        target = self.target_for(case)
        sandbox = DockerSandbox(SandboxLimits(image=target.sandbox_image, timeout_seconds=90))
        with wt.worktree(target.repo_path) as tree:
            wt.write_file(tree, patched_path, patch_code)
            wt.write_file(tree, target.hidden_test_path, hidden)
            result = await sandbox.run_pytest(worktree=str(tree), target=target.hidden_test_path)
        passed = result["exit_code"] == 0
        detail = "" if passed else f"{result['stdout']}\n{result['stderr']}"[-800:]
        return passed, detail

    def _hidden_test(self, case: Case) -> str | None:
        """Load a case's hidden test, if it has one.

        Searched across every dataset rather than one, because a split mixes
        datasets and looking in only one of them would silently report half the
        patches as unjudged.
        """
        if self.datasets_root is None or not self.datasets_root.is_dir():
            return None
        for directory in sorted(self.datasets_root.iterdir()):
            path = directory / HIDDEN_DIR_NAME / f"{case.id}.py"
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None


def prepare_repo(source: Path, destination: Path) -> Path:
    """Copy the target repository somewhere the evaluation can patch freely."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


__all__ = ["CaseResult", "MemoryApps", "Runner", "RunnerReport", "prepare_repo"]
