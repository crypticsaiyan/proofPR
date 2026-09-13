"""The state machine.

The only place steps are sequenced, and the only place writes are decided. The
model contributes data at two narrow points (intent, duplicate verdict) and
chooses nothing.

Order is cheapest first, exactly as AGENTS.md section 3.2 specifies: sanitize,
pre-check, enrich, exists, worth-it. Everything from reproduction onward lands in
M3; until then a report that survives every check is filed for a human and the
run ends with `triaged`, which is an honest description of what was done rather
than a claim about code.

Every step writes `step_started` before acting and `step_finished` after, with a
full state snapshot, so a crash anywhere is resumable and every number the
evaluation reports comes from the ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofpr.domain.enums import Arm, Outcome, Reason, Step
from proofpr.domain.errors import GuardBlockedError, ProofPRError, RetryableAppError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.domain.run import ExistsFinding, Report, RunState
from proofpr.guard.sanitize import sanitize
from proofpr.ledger import Ledger, LedgerError
from proofpr.observability.logging import bind_run, clear_run, get_logger
from proofpr.prompts import DUPLICATE_SYSTEM, DUPLICATE_USER, INTENT_SYSTEM, INTENT_USER
from proofpr.proof import checks as proof_checks
from proofpr.proof import mutator
from proofpr.steps import approve as approve_step
from proofpr.steps import ci_wait, repro_raw, reproduce
from proofpr.steps import exists as exists_step
from proofpr.steps import patch as patch_step
from proofpr.steps import publish as publish_step
from proofpr.steps import verify as verify_step
from proofpr.steps import worktree as wt
from proofpr.triage import fingerprint as fp
from proofpr.triage import intent as intent_rules
from proofpr.triage import scope_rules, version_range

logger = get_logger("proofpr.pipeline")


class Stop(Exception):  # noqa: N818 - control flow, not an error condition
    """Raised inside a step to end the run with a terminal state.

    Not an error. Most runs end this way, and ending early is the product
    working rather than failing.
    """

    def __init__(
        self,
        outcome: Outcome,
        reason: Reason | None,
        message: str,
        state: RunState | None = None,
    ) -> None:
        """Record the terminal state and the message shown to the reporter.

        Args:
            outcome: The terminal state.
            reason: The reason code, when the outcome has one.
            message: What the reporter is told, verbatim.
            state: The state as of the stopping step. Carried because a step
                that stops has usually just learned the most important thing
                about the run, and losing it would leave the issue and the
                evaluation without the finding that ended the run.
        """
        super().__init__(message)
        self.outcome = outcome
        self.reason = reason
        self.message = message
        self.state = state


def _package(repo: dict[str, Any]) -> str:
    """Read `repo.package`, accepting one name or a list of top-level modules."""
    value = repo.get("package", repo.get("name", ""))
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


@dataclass
class Config:
    """Pipeline configuration read from `config/proofpr.toml`."""

    supported_versions: str = ">=0.0.0"
    branch_prefix: str = "proofpr/"
    ci_poll_seconds: int = 15
    ci_timeout_minutes: int = 20
    patch_paths: tuple[str, ...] = ("src/**",)
    fix_classes: tuple[str, ...] = scope_rules.DEFAULT_FIX_CLASSES
    duplicate_threshold: float = 0.62
    default_branch: str = "main"
    linear_team_id: str = ""
    #: Local checkout of the target repository. Reproduction is skipped without
    #: one, and the run says so rather than pretending it reproduced nothing.
    repo_path: Path | None = None
    package: str = ""
    sandbox_timeout_seconds: int = 60
    #: Attempts per write, including the first. Each retry first asks the
    #: application whether the previous attempt landed.
    write_attempts: int = 3
    #: First delay between write attempts, doubled each time.
    write_backoff_seconds: float = 1.0

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Config:
        """Build configuration from a parsed TOML document."""
        repo = document.get("repo", {})
        fix_class = document.get("fix_class", {})
        triage = document.get("triage", {})
        local = repo.get("local_path")
        ci = document.get("ci", {})
        return cls(
            supported_versions=str(repo.get("supported_versions", ">=0.0.0")),
            patch_paths=tuple(fix_class.get("patch_paths", ("src/**",))),
            fix_classes=tuple(fix_class.get("allowed", scope_rules.DEFAULT_FIX_CLASSES)),
            duplicate_threshold=float(triage.get("duplicate_score_threshold", 0.62)),
            default_branch=str(repo.get("default_branch", "main")),
            branch_prefix=str(repo.get("branch_prefix", "proofpr/")),
            ci_poll_seconds=int(ci.get("poll_interval_seconds", 15)),
            ci_timeout_minutes=int(ci.get("timeout_minutes", 20)),
            repo_path=Path(str(local)) if local else None,
            package=_package(repo),
            sandbox_timeout_seconds=int(document.get("sandbox", {}).get("timeout_seconds", 60)),
            write_attempts=int(document.get("writes", {}).get("attempts", 3)),
            write_backoff_seconds=float(document.get("writes", {}).get("backoff_seconds", 1.0)),
        )


class ModelJudge:
    """Adapts the model port to the narrow questions the pipeline asks it."""

    def __init__(self, model: Any, ledger: Ledger, run_id: str) -> None:  # noqa: ANN401
        """Bind a model client to one run, so every call is costed against it."""
        self._model = model
        self._ledger = ledger
        self._run_id = run_id

    async def classify_intent(self, block: str, step: Step) -> intent_rules.IntentVerdict:
        """Ask the one intent question."""
        verdict, usage = await self._model.complete_json(
            system=INTENT_SYSTEM,
            user=INTENT_USER.format(block=block),
            schema=intent_rules.IntentVerdict,
            tier="cheap",
            max_tokens=300,
        )
        self._record(usage, step)
        assert isinstance(verdict, intent_rules.IntentVerdict)  # noqa: S101 - narrows the port's Any
        return verdict

    async def same_defect(self, report: str, candidate: str) -> tuple[bool, float, str]:
        """Ask whether two reports describe the same defect."""
        from pydantic import BaseModel, Field

        class Verdict(BaseModel):
            same_defect: bool
            confidence: float = Field(ge=0.0, le=1.0)
            rationale: str = Field(default="", max_length=300)

        verdict, usage = await self._model.complete_json(
            system=DUPLICATE_SYSTEM,
            user=DUPLICATE_USER.format(report=report, candidate=candidate),
            schema=Verdict,
            tier="cheap",
            max_tokens=300,
        )
        self._record(usage, Step.EXISTS)
        return verdict.same_defect, verdict.confidence, verdict.rationale

    def _record(self, usage: Any, step: Step) -> None:  # noqa: ANN401
        """Write the call's usage and cost to the ledger."""
        self._ledger.record_model_call(
            self._run_id,
            step=step,
            tier=usage.tier,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )


class Pipeline:
    """Runs one report through the triage stages."""

    def __init__(
        self,
        *,
        apps: Any,  # noqa: ANN401 - an Apps bundle or any object with the four ports
        ledger: Ledger,
        model: Any = None,  # noqa: ANN401
        config: Config | None = None,
        approver: Any = None,  # noqa: ANN401 - an ApprovalPort
        arm: Arm = Arm.PROOFPR,
    ) -> None:
        """Build a pipeline over already-constructed ports.

        An absent approver means nothing is published. That is the safe default:
        an agent with no one to ask must not push to someone's repository.
        """
        self.apps = apps
        self.ledger = ledger
        self.model = model
        self.config = config or Config()
        self.approver = approver
        self.arm = arm

    async def run(self, report: Report, *, run_id: str | None = None) -> RunState:
        """Triage one report from the beginning and return its terminal state."""
        identifier = self.ledger.start_run(source=report.source, run_id=run_id, arm=self.arm)
        return await self._execute(
            RunState(run_id=identifier, report=report, arm=self.arm), resuming=False
        )

    async def resume(self, run_id: str) -> RunState:
        """Continue a run that died, from the last step that finished.

        The ledger holds a full state snapshot after every step, so resuming is
        a matter of replaying nothing: the state is read, the completed steps are
        skipped, and the step the run died inside is re-entered. Re-entry is safe
        because every write is recorded before it is attempted and skipped if it
        already succeeded.

        Raises:
            LedgerError: There is no such run, or it already finished.
        """
        row = self.ledger.get_run(run_id)
        if row is None:
            raise LedgerError(f"no run {run_id} in this ledger")
        if row["finished_at"]:
            raise LedgerError(f"run {run_id} already finished as {row['outcome']}")

        stopped = self._recorded_stop(run_id)
        if stopped is not None:
            # The run had already decided how it ends and died while telling the
            # applications. Re-entering the plan would carry on past that
            # decision, so the only thing left to do is finish telling them.
            stop, notify, anchor = stopped
            assert stop.state is not None  # noqa: S101 - _recorded_stop always restores one
            self.ledger.append(
                run_id,
                "resumed",
                {"restored_after": "run_stopped", "reentering": "finish", "completed": []},
            )
            logger.info("run_resumed", run_id=run_id, restored_after="run_stopped")
            bind_run(run_id)
            try:
                return await self._conclude(
                    stop.state, stop, notify=notify, record=False, anchor=anchor
                )
            finally:
                clear_run()

        state = self._restore(run_id)
        reentering = self.ledger.resume_point(run_id)
        self.ledger.append(
            run_id,
            "resumed",
            {
                # Two different facts, and conflating them makes a resume log
                # impossible to read: the state came from the last step that
                # finished, and execution re-enters the step that did not.
                "restored_after": state.step.value,
                "reentering": reentering.value if reentering else None,
                "completed": sorted(step.value for step in self._completed(run_id)),
            },
        )
        logger.info(
            "run_resumed",
            run_id=run_id,
            restored_after=state.step.value,
            reentering=reentering.value if reentering else None,
        )
        return await self._execute(state, resuming=True)

    def _restore(self, run_id: str) -> RunState:
        """Rebuild run state from the last snapshot in the ledger.

        Raises:
            LedgerError: No step ever finished, so there is nothing to resume.
        """
        for event in reversed(self.ledger.events(run_id)):
            if event["kind"] == "step_finished" and event["payload"]:
                return RunState.model_validate(event["payload"])
        raise LedgerError(f"run {run_id} has no completed step to resume from")

    def _recorded_stop(self, run_id: str) -> tuple[Stop, bool, str] | None:
        """Return the stop decision a run recorded before it died, if any.

        The returned anchor is that event's own chain hash: a value fixed the
        moment the run decided its outcome, before any notification write could
        touch it. Recomputing the chain head fresh on every replay was what made
        the receipt drift on every retry, which was also what made the retry
        write a second copy: the two attempts described the "same" write with
        two different bodies, and idempotency is matched by content.
        """
        for event in reversed(self.ledger.events(run_id)):
            if event["kind"] == "run_stopped":
                payload = event["payload"]
                stop = Stop(
                    Outcome(payload["outcome"]),
                    Reason(payload["reason"]) if payload.get("reason") else None,
                    str(payload["message"]),
                    RunState.model_validate(payload["state"]),
                )
                return stop, bool(payload.get("notify", True)), str(event["chain_hash"])
        return None

    def _completed(self, run_id: str) -> set[Step]:
        """Return the steps that finished, and are therefore not re-run."""
        return {
            Step(event["step"])
            for event in self.ledger.events(run_id)
            if event["kind"] == "step_finished" and event["step"]
        }

    async def _execute(self, state: RunState, *, resuming: bool) -> RunState:
        """Run the step plan, skipping anything already finished."""
        bind_run(state.run_id)
        judge = ModelJudge(self.model, self.ledger, state.run_id) if self.model else None
        done = self._completed(state.run_id) if resuming else set()

        plan: list[tuple[Step, Any, dict[str, Any]]] = [
            (Step.SANITIZE, self._sanitize, {}),
            (Step.PRE_CHECK, self._pre_check, {"judge": judge}),
            (Step.FINGERPRINT, self._fingerprint_report, {}),
            (Step.EXISTS, self._exists, {"judge": judge}),
            (Step.WORTH_IT, self._worth_it, {}),
            (Step.REPRO_RAW, self._reproduce, {}),
            (Step.PATCH, self._patch_and_prove, {}),
            (Step.APPROVAL, self._approve, {}),
            (Step.PUBLISH, self._publish, {}),
            (Step.CI_WAIT, self._wait_for_ci, {}),
        ]
        if state.arm is Arm.NO_GATE:
            plan = [
                (Step.SANITIZE, self._sanitize, {}),
                (Step.PRE_CHECK, self._pre_check, {"judge": judge}),
                (Step.FINGERPRINT, self._fingerprint_report, {}),
                (Step.LOCALIZE, self._localize_only, {}),
                (Step.PATCH, self._patch_without_proof, {}),
                (Step.APPROVAL, self._approve, {}),
                (Step.PUBLISH, self._publish, {}),
                (Step.CI_WAIT, self._wait_for_ci, {}),
            ]

        try:
            for step, handler, kwargs in plan:
                if step in done:
                    logger.info("step_skipped_on_resume", step=step.value)
                    continue
                if step is Step.APPROVAL and state.arm is Arm.PROOFPR and not state.proof_complete:
                    # Reproduction or patching stopped short of a publishable
                    # proof, so the run ends here rather than asking anyone to
                    # approve something that was never proved.
                    _stop_triage_only(state)
                state = await self._step(state, step, handler, **kwargs)
            _stop_pr_opened(state)
        except Stop as stop:
            return await self._conclude(stop.state or state, stop)
        except GuardBlockedError as blocked:
            self.ledger.append(
                state.run_id,
                "guard_blocked",
                {"operation": blocked.operation, "rule": blocked.rule},
                step=state.step,
            )
            return await self._conclude(
                state,
                Stop(Outcome.FAILED_CLOSED, Reason.INJECTION_ONLY, str(blocked)),
                notify=False,
            )
        except RetryableAppError as error:
            return self._pause(state, error)
        except ProofPRError as error:
            self.ledger.append(
                state.run_id,
                "step_failed",
                {"error": str(error), "code": error.code},
                step=state.step,
            )
            return await self._conclude(
                state, Stop(Outcome.FAILED_CLOSED, None, str(error)), notify=False
            )
        finally:
            clear_run()
        raise AssertionError("unreachable: every path above returns")

    # -- step plumbing --------------------------------------------------------

    async def _step(self, state: RunState, step: Step, handler: Any, **kwargs: Any) -> RunState:  # noqa: ANN401
        """Run one step, bracketed by ledger events and a state snapshot."""
        if state.arm.value == "no_gate" and step in _SKIPPED_BY_BASELINE:
            self.ledger.append(state.run_id, "step_skipped", {"arm": state.arm.value}, step=step)
            return state.advanced(step)

        self.ledger.append(state.run_id, "step_started", step=step)
        try:
            updated: RunState = await handler(state.advanced(step), **kwargs)
        except Stop as stop:
            # A step that stops the run has finished its work. Recording it as
            # unfinished would make every early exit look like a crash to the
            # resume logic, and most runs end early by design.
            self.ledger.append(
                state.run_id,
                "step_finished",
                (stop.state or state).snapshot(),
                step=step,
            )
            raise
        self.ledger.append(state.run_id, "step_finished", updated.snapshot(), step=step)
        return updated

    # -- steps ----------------------------------------------------------------

    async def _sanitize(self, state: RunState) -> RunState:
        """Wrap the report as untrusted and record any injection flags."""
        result = sanitize(state.report.text, source=f"{state.report.source}.report")
        if result.flagged:
            self.ledger.append(
                state.run_id, "injection_flagged", {"flags": list(result.flags)}, step=Step.SANITIZE
            )
        return state.advanced(
            Step.SANITIZE, injection_flags=list(result.flags), sanitized_text=result.block()
        )

    async def _pre_check(self, state: RunState, judge: ModelJudge | None) -> RunState:
        """Classify intent, rules first, one narrow model call second."""
        decision = intent_rules.rule_check(state.report.text)
        if decision is None and judge is not None:
            decision = intent_rules.from_verdict(
                await judge.classify_intent(state.sanitized_text, Step.PRE_CHECK),
                state.report.text,
            )
        if decision is None:
            # No model available and no rule fired. Continue: a missed greeting
            # costs one search, a missed bug report costs a user their bug.
            decision = intent_rules.PreCheck(
                intent=intent_rules.Intent.BUG,
                confidence=0.0,
                decided_by="default",
                rationale="no rule matched and no model was configured",
            )

        updated = state.advanced(
            Step.PRE_CHECK,
            intent=decision.intent.value,
            intent_confidence=decision.confidence,
            intent_decided_by=decision.decided_by,
            has_error_output=decision.has_error_output,
        )
        if not decision.should_continue:
            raise Stop(
                Outcome.NOT_A_BUG,
                Reason.NOT_A_BUG_REPORT,
                f"This looks like a {decision.intent.value} rather than a bug report, "
                f"so no issue was filed. {decision.rationale}",
                updated,
            )
        return updated

    async def _fingerprint_report(self, state: RunState) -> RunState:
        """Fingerprint the report: exception, file, function, and any stated version."""
        fingerprint = fp.from_text(state.report.text)
        version = version_range.find_in_text(state.report.text)

        return state.advanced(
            Step.FINGERPRINT,
            exception=fingerprint.exception,
            path=fingerprint.path,
            function=fingerprint.function,
            fingerprint_digest=fingerprint.digest,
            reported_version=str(version) if version else None,
        )

    async def _exists(self, state: RunState, judge: ModelJudge | None) -> RunState:
        """Search every application before doing any work."""
        finding = await exists_step.check(
            fingerprint=self._fingerprint(state),
            report_text=state.report.text,
            linear=self.apps.linear,
            github=self.apps.github,
            judge=judge,
            reported_version=state.reported_version,
            default_branch=self.config.default_branch,
            threshold=self.config.duplicate_threshold,
        )
        updated = state.advanced(Step.EXISTS, exists=finding)
        if finding.found:
            outcome, reason, message = _EXISTS_OUTCOMES[finding.kind](finding)
            raise Stop(outcome, reason, message, updated)
        return updated

    async def _worth_it(self, state: RunState) -> RunState:
        """Apply the rules-only scope decision."""
        decision = scope_rules.decide(
            self._fingerprint(state),
            reported_version=state.reported_version,
            supported=self.config.supported_versions,
            patch_paths=self.config.patch_paths,
            fix_classes=self.config.fix_classes,
        )
        updated = state.advanced(
            Step.WORTH_IT,
            in_scope=decision.in_scope,
            fix_class=decision.fix_class,
            scope_detail=decision.detail,
        )
        if not decision.in_scope:
            raise Stop(
                Outcome.OUT_OF_SCOPE,
                decision.reason,
                f"Filed for a human: {decision.detail}. This agent did not attempt a fix.",
                updated,
            )
        return updated

    async def _reproduce(self, state: RunState) -> RunState:
        """Run both reproduction stages inside one worktree.

        Stage 1 establishes that the reported input really crashes, and what it
        crashes with. Stage 2 converts that captured traceback into a test that
        must pass the gate. Both stages share a worktree, because stage 2 needs
        the source of the frame stage 1 landed in.
        """
        # A configured path that does not exist is treated the same as no path at
        # all. Failing the run closed would punish the reporter for a deployment
        # mistake, and the issue is still worth filing either way.
        if self.config.repo_path is None or not self.config.repo_path.is_dir():
            missing = (
                f" (configured checkout {self.config.repo_path} does not exist)"
                if self.config.repo_path is not None
                else ""
            )
            raise Stop(
                Outcome.TRIAGED,
                None,
                "In scope and worth fixing. Filed for a human: this configuration has no "
                f"target repository checkout, so nothing was reproduced{missing}.",
                state,
            )

        with wt.worktree(self.config.repo_path) as tree:
            raw = await repro_raw.run(
                sandbox=self.apps.sandbox,
                worktree_path=tree,
                report_text=state.report.text + (state.clarify_answer or ""),
                package=self.config.package,
                timeout_seconds=self.config.sandbox_timeout_seconds,
            )
            state = state.advanced(
                Step.REPRO_RAW,
                repro_outcome=raw.outcome.value,
                repro_origin=raw.origin or None,
                repro_code=raw.code or None,
                repro_traceback=raw.traceback[-4000:],
                repro_exception=raw.exception,
            )
            self.ledger.append(
                state.run_id,
                "repro_result",
                {"outcome": raw.outcome.value, "exception": raw.exception, "origin": raw.origin},
                step=Step.REPRO_RAW,
            )

            if not raw.reproduced:
                # Stage 1 needs no model, and neither does the fallback question,
                # so the clarify path works in a configuration with no model at
                # all. Only test synthesis below actually requires one.
                await self._clarify(state, raw)

            if self.model is None:
                raise Stop(
                    Outcome.CONFIRMED_NO_TEST,
                    None,
                    f"Reproduced the crash ({raw.exception}) by running the reported input. "
                    "No model is configured, so no test was written. Filed with the traceback.",
                    state,
                )

            synth = await reproduce.synthesize(
                model=self.model,
                sandbox=self.apps.sandbox,
                worktree_path=tree,
                raw=raw,
                sanitized_block=state.sanitized_text,
                package=self.config.package,
                record_usage=self._usage_recorder(state, Step.TEST_SYNTH),
                timeout_seconds=self.config.sandbox_timeout_seconds,
            )

        self.ledger.append(
            state.run_id,
            "gate_result",
            {
                "passed": synth.succeeded,
                "attempts": synth.attempts,
                "rejections": [rejection.rule for rejection in synth.rejections],
            },
            step=Step.GATE,
        )

        if not synth.succeeded:
            stopped = state.advanced(
                Step.TEST_SYNTH, test_attempts=synth.attempts, gate_passed=False
            )
            raise Stop(
                Outcome.CONFIRMED_NO_TEST,
                # `is not None`, not truthiness: a GateResult is falsy exactly
                # when the gate failed, which is the case that has a reason.
                synth.gate.reason if synth.gate is not None else Reason.NO_FAILING_TEST,
                f"Reproduced the crash ({raw.exception}) but could not express it as a test in "
                f"{synth.attempts} attempts. Filed with the traceback attached.",
                stopped,
            )

        assert synth.test is not None  # noqa: S101 - guaranteed by `succeeded`
        return state.advanced(
            Step.GATE,
            test_path=synth.test.path,
            test_code=synth.test.code,
            test_attempts=synth.attempts,
            gate_passed=True,
            gate_checks=dict(synth.gate.checks) if synth.gate is not None else {},
        )

    async def _patch_and_prove(self, state: RunState) -> RunState:
        """Write a patch, then prove it, in one worktree.

        The patch loop and the proof checks share a worktree because the proof is
        about this patch: reverting it, mutating its own changed lines, and
        rerunning the test it was written to satisfy.
        """
        if self.config.repo_path is None or self.model is None:
            return state

        with wt.worktree(self.config.repo_path) as tree:
            patch_result = await patch_step.run(
                model=self.model,
                sandbox=self.apps.sandbox,
                policy=self.apps.guard.policy,
                worktree_path=tree,
                test_path=str(state.test_path),
                test_code=str(state.test_code),
                traceback=state.repro_traceback,
                source_path=str(state.path or ""),
                sanitized_block=state.sanitized_text,
                record_usage=self._usage_recorder(state, Step.PATCH),
                timeout_seconds=self.config.sandbox_timeout_seconds,
            )
            self.ledger.append(
                state.run_id,
                "patch_result",
                {
                    "accepted": patch_result.succeeded,
                    "attempts": [
                        {"n": attempt.number, "accepted": attempt.accepted, "why": attempt.detail}
                        for attempt in patch_result.attempts
                    ],
                },
                step=Step.PATCH,
            )

            if not patch_result.succeeded or patch_result.patch is None:
                last = patch_result.attempts[-1].detail if patch_result.attempts else "no attempt"
                raise Stop(
                    Outcome.ABANDONED,
                    None,
                    f"Reproduced it and wrote a failing test, but could not fix it in "
                    f"{len(patch_result.attempts)} attempts. Filed with the test attached. "
                    f"Last attempt: {last[:200]}",
                    state.advanced(Step.PATCH, patch_attempts=len(patch_result.attempts)),
                )

            patched = {file.path: file.content for file in patch_result.patch.files}
            report = proof_checks.ProofReport(
                test_failed_on_base=True,
                base_failure=str(state.repro_exception or ""),
                test_passes_with_patch=True,
                suite_passes_with_patch=True,
            )
            report = await proof_checks.run(
                sandbox=self.apps.sandbox,
                worktree_path=tree,
                test_path=str(state.test_path),
                patched_files=patched,
                originals=patch_result.originals,
                report=report,
                timeout_seconds=self.config.sandbox_timeout_seconds,
            )

        self.ledger.append(
            state.run_id,
            "proof_result",
            {
                "complete": report.complete,
                "revert_reintroduces": report.revert_reintroduces,
                "flake_runs_passed": report.flake_runs_passed,
                "mutants": len(report.mutants),
                "killed": report.mutants_killed,
                "survivors": [mutant.label for mutant in report.survivors],
            },
            step=Step.PROOF,
        )

        patched_path = next(iter(patched))
        block = proof_checks.render_block(
            report,
            test_path=str(state.test_path),
            exception=state.repro_exception,
            run_id=state.run_id,
        )
        updated = state.advanced(
            Step.PROOF,
            patch_attempts=len(patch_result.attempts),
            patch_summary=patch_result.patch.summary,
            patch_rationale=patch_result.patch.rationale,
            patch_code=patched[patched_path],
            patched_path=patched_path,
            patch_diff_lines=len(
                mutator.changed_lines(
                    patch_result.originals.get(patched_path) or "", patched[patched_path]
                )
            ),
            proof_complete=report.complete,
            proof_block=block,
            mutants_killed=report.mutants_killed,
            mutants_total=len(report.mutants),
        )

        if not report.complete:
            raise Stop(
                Outcome.ABANDONED,
                None,
                f"Wrote a patch that passes the test and the suite, but the proof is "
                f"incomplete: {report.failure}. Not publishing it. Filed for a human.",
                updated,
            )
        return updated

    async def _localize_only(self, state: RunState) -> RunState:
        """The baseline's substitute for the checks it skips.

        A report with no identifiable frame cannot be patched by anyone, so even
        the baseline stops here. Everything else the full pipeline would have
        checked, the baseline does not.
        """
        if not state.path:
            raise Stop(
                Outcome.OUT_OF_SCOPE,
                Reason.INSUFFICIENT_REPORT,
                "No stack frame to localize, so there is nothing to patch.",
                state,
            )
        return state.advanced(Step.LOCALIZE)

    async def _patch_without_proof(self, state: RunState) -> RunState:
        """Patch from the report alone, which is what the baseline arm measures.

        No reproduction, no failing test, no revert check, no mutants. The patch
        is accepted if the project's own suite still passes, which is the bar a
        competent tool-calling agent clears today and precisely the bar this
        project argues is not enough.
        """
        if self.config.repo_path is None or self.model is None:
            raise Stop(
                Outcome.TRIAGED,
                None,
                "No target repository checkout or no model in this configuration.",
                state,
            )

        with wt.worktree(self.config.repo_path) as tree:
            result = await patch_step.run_without_test(
                model=self.model,
                sandbox=self.apps.sandbox,
                policy=self.apps.guard.policy,
                worktree_path=tree,
                report_text=state.report.text,
                sanitized_block=state.sanitized_text,
                source_path=str(state.path or ""),
                exception=state.exception,
                record_usage=self._usage_recorder(state, Step.PATCH),
                timeout_seconds=self.config.sandbox_timeout_seconds,
            )
            self.ledger.append(
                state.run_id,
                "patch_result",
                {"accepted": result.succeeded, "arm": state.arm.value},
                step=Step.PATCH,
            )
            if not result.succeeded or result.patch is None:
                raise Stop(
                    Outcome.ABANDONED,
                    None,
                    "Could not write a patch that keeps the suite green.",
                    state.advanced(Step.PATCH, patch_attempts=len(result.attempts)),
                )
            patched = {file.path: file.content for file in result.patch.files}

        path = next(iter(patched))
        # The baseline still writes a body, and it still claims a fix. What it
        # cannot do is prove one, which is the difference the evaluation reports.
        block = (
            "## Summary\n\n"
            f"{result.patch.rationale or result.patch.summary}\n\n"
            "_No reproduction test, no revert check, no mutation check._"
        )
        return state.advanced(
            Step.PATCH,
            patch_attempts=len(result.attempts),
            patch_summary=result.patch.summary,
            patch_rationale=result.patch.rationale,
            patch_code=patched[path],
            patched_path=path,
            proof_complete=False,
            proof_block=block,
            test_path=None,
            test_code=None,
        )

    async def _approve(self, state: RunState) -> RunState:
        """Ask whether this may be published."""
        decision = await approve_step.request_approval(
            self.approver,
            approve_step.ApprovalRequest(
                run_id=state.run_id,
                summary=state.patch_summary or "",
                proof_block=state.proof_block or "",
                patched_path=state.patched_path or "",
                test_path=state.test_path or "",
                diff_lines=state.patch_diff_lines,
                report_url=state.report.url,
            ),
        )
        self.ledger.append(
            state.run_id,
            "approval_received",
            {"approved": decision.approved, "by": decision.decided_by, "why": decision.reason},
            step=Step.APPROVAL,
        )
        updated = state.advanced(Step.APPROVAL, approved_by=decision.decided_by)
        if not decision.approved:
            raise Stop(
                Outcome.REJECTED_BY_MAINTAINER,
                None,
                f"Reproduced, fixed, and proved, but publication was not approved: "
                f"{decision.reason or 'declined'}. Filed with the proof attached.",
                updated,
            )
        return updated

    async def _publish(self, state: RunState) -> RunState:
        """Create the branch, commit the test and the fix, open the draft."""
        files = {}
        if state.test_path and state.test_code:
            files[str(state.test_path)] = str(state.test_code)
        files[str(state.patched_path)] = str(state.patch_code)
        published = await publish_step.run(
            github=self.apps.github,
            write=lambda intent: self._write(state, self._method(intent), intent),
            intent_factory=lambda operation, *, target, **payload: self._intent(
                state, operation, target=target, **payload
            ),
            run_id=state.run_id,
            branch_prefix=self.config.branch_prefix,
            default_branch=self.config.default_branch,
            files=files,
            commit_summary=state.patch_summary or "",
            exception=state.repro_exception,
            title=publish_step.pr_title(
                state.patch_summary or "", state.repro_exception, state.function
            ),
            body=publish_step.pr_body(
                proof_block=state.proof_block or "",
                rationale=state.patch_rationale or "",
                report_url=state.report.url,
                linear_url=state.linear_url,
                linear_identifier=state.linear_identifier,
                run_id=state.run_id,
            ),
        )
        return state.advanced(
            Step.PUBLISH,
            branch=published.branch,
            pr_number=published.pr_number,
            pr_url=published.pr_url,
            head_sha=published.head_sha,
        )

    async def _wait_for_ci(self, state: RunState) -> RunState:
        """Wait for CI, then take the pull request out of draft if it passed."""
        result = await ci_wait.wait(
            github=self.apps.github,
            ref=str(state.head_sha),
            poll_seconds=self.config.ci_poll_seconds,
            timeout_minutes=self.config.ci_timeout_minutes,
        )
        self.ledger.append(
            state.run_id,
            "ci_result",
            {"outcome": result.outcome.value, "waited": result.waited_seconds},
            step=Step.CI_WAIT,
        )
        updated = state.advanced(
            Step.CI_WAIT, ci_outcome=result.outcome.value, ci_summary=result.summary
        )

        if not result.succeeded:
            raise Stop(
                Outcome.CI_FAILED,
                None,
                f"Opened {state.pr_url} but it stays in draft: {result.summary}.",
                updated,
            )

        ready = await self._write(
            updated,
            self.apps.github.mark_ready,
            self._intent(
                updated,
                "github.mark_ready",
                target=f"{self.apps.github.slug}#{updated.pr_number}",
                number=updated.pr_number,
                ci_success=True,
            ),
        )
        return updated.advanced(Step.CI_WAIT, pr_ready=bool(ready.verified))

    def _method(self, intent: WriteIntent) -> Any:  # noqa: ANN401 - an adapter method
        """Resolve the adapter method an intent names."""
        adapter = getattr(self.apps, intent.app)
        method: Any = getattr(adapter, intent.short_operation)
        return method

    def _usage_recorder(self, state: RunState, step: Step) -> Callable[[Any], None]:
        """Return a callback that books a model call's cost against this run."""

        def record(usage: Any) -> None:  # noqa: ANN401 - the port's usage record
            self.ledger.record_model_call(
                state.run_id,
                step=step,
                tier=usage.tier,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
            )

        return record

    async def _clarify(self, state: RunState, raw: repro_raw.RawRepro) -> None:
        """Ask the reporter one question, then stop and wait for their answer.

        The run ends here rather than blocking. An answer arrives as a new
        message, and the reply carries the run marker, so resuming is a lookup
        rather than a held connection.

        Raises:
            Stop: Always. This path never continues in the same run.
        """
        question = reproduce.fallback_question(raw)
        if self.model is not None:
            # A model outage must not cost the reporter their question: the
            # rule-based fallback above is already sendable.
            with contextlib.suppress(ProofPRError):
                question = await reproduce.ask_clarifying_question(
                    model=self.model,
                    sanitized_block=state.sanitized_text,
                    attempted=f"ran `{raw.code}`, got {raw.outcome.value}",
                    record_usage=self._usage_recorder(state, Step.CLARIFY),
                )

        self.ledger.append(state.run_id, "clarify_sent", {"question": question}, step=Step.CLARIFY)
        raise Stop(
            Outcome.DECLINED,
            raw.reason or Reason.REPRO_NO_CRASH,
            question,
            state.advanced(Step.CLARIFY, clarify_question=question),
        )

    # -- terminal -------------------------------------------------------------

    async def _conclude(
        self,
        state: RunState,
        stop: Stop,
        *,
        notify: bool = True,
        record: bool = True,
        anchor: str | None = None,
    ) -> RunState:
        """Record how the run ends, then finish it.

        The decision is written before any terminal write is attempted. A run
        that dies while filing its issue is resumed into finishing that decline,
        not back into the plan: without this record, resume saw only finished
        steps and carried on to reproduce, patch, and open a pull request for a
        report the run had already ruled out of scope.

        `anchor` is the receipt: the chain hash of the `run_stopped` event
        itself, fixed once and reused on every replay so the receipt embedded in
        outward-facing writes never changes underneath a retry.
        """
        if record:
            anchor = self.ledger.append(
                state.run_id,
                "run_stopped",
                {
                    "outcome": stop.outcome.value,
                    "reason": stop.reason.value if stop.reason else None,
                    "message": stop.message,
                    "notify": notify,
                    "state": state.snapshot(),
                },
                step=state.step,
            )
        assert anchor is not None  # noqa: S101 - record=False always supplies one
        try:
            return await self._finish(state, stop, notify=notify, anchor=anchor)
        except RetryableAppError as error:
            return self._pause(state, error)

    def _pause(self, state: RunState, error: RetryableAppError) -> RunState:
        """Leave the run unfinished after an application stayed unavailable.

        A transient failure is not a verdict on the report. Closing the run as
        failed would throw away a decision the pipeline already paid for, so the
        run is left resumable and `proofpr resume` continues it once the
        application is back. Every write already made is recorded, so resuming
        repeats none of them.
        """
        self.ledger.append(
            state.run_id,
            "run_paused",
            {"error": str(error), "app": error.app, "status": error.status},
            step=state.step,
        )
        logger.warning("run_paused", step=state.step.value, app=error.app, error=str(error))
        return state

    async def _finish(
        self, state: RunState, stop: Stop, *, notify: bool = True, anchor: str
    ) -> RunState:
        """File the issue of record, tell the reporter, and seal the run.

        `anchor` is published as the receipt and is also what `finish_run` seals
        the run with, so the value printed in the pull request, the issue, and
        the thread is the exact value `proofpr verify-receipt` checks against,
        not a different one computed moments later after further events have
        extended the chain.
        """
        state = state.advanced(state.step, outcome=stop.outcome, reason=stop.reason)

        if notify and stop.outcome is not Outcome.NOT_A_BUG:
            state = await self._file_issue(state, stop)
        if notify:
            state = await self._reply(state, stop)
        if notify and state.pr_url and state.linear_issue_id:
            state = await self._attach_pr(state)
        if notify:
            state = await self._verify_consistency(state, stop)
        if notify:
            state = await self._publish_receipt(state, anchor)

        # The terminal phase writes the issue, the reply, the attachment and the
        # receipt, all after the last step snapshot. Without a final snapshot the
        # reconciler and the report would see a state that stops before the run
        # did, and would conclude the writes never happened.
        self.ledger.append(state.run_id, "state_final", state.snapshot(), step=state.step)

        receipt = self.ledger.finish_run(
            state.run_id, outcome=stop.outcome, reason=stop.reason, receipt=anchor
        )
        logger.info(
            "run_finished",
            outcome=stop.outcome.value,
            reason=stop.reason.value if stop.reason else None,
            cost_usd=self.ledger.total_cost(state.run_id),
        )
        return state.advanced(state.step, receipt=receipt)

    async def _file_issue(self, state: RunState, stop: Stop) -> RunState:
        """Create or comment on the Linear issue of record."""
        if state.exists.kind == "duplicate" and state.exists.app == "linear":
            intent = self._intent(
                state,
                "linear.comment",
                target=str(state.exists.reference),
                issue_id=str(state.exists.reference),
                body=(
                    f"Another reporter hit this.\n\n{state.report.url or state.report.source}\n\n"
                    f"Matched by: {state.exists.evidence}"
                ),
            )
            result = await self._write(state, self.apps.linear.comment, intent)
            return state.advanced(state.step, linear_issue_id=str(state.exists.reference))

        intent = self._intent(
            state,
            "linear.create_issue",
            target=self.config.linear_team_id or "team",
            title=self._issue_title(state),
            description=self._issue_body(state, stop),
        )
        result = await self._write(state, self.apps.linear.create_issue, intent)
        return state.advanced(
            state.step,
            linear_issue_id=result.remote_id,
            linear_identifier=str(result.observed.get("identifier") or result.remote_id),
            linear_url=result.url,
        )

    async def _reply(self, state: RunState, stop: Stop) -> RunState:
        """Reply to the reporter in their own thread."""
        if state.report.channel_id is None:
            return state
        lines = [stop.message]
        if state.exists.url:
            lines.append(f"Existing work: {state.exists.url}")
        if state.linear_url:
            lines.append(f"Tracked as {state.linear_identifier}: {state.linear_url}")
        intent = self._intent(
            state,
            "discord.reply",
            target=str(state.report.channel_id),
            channel_id=state.report.channel_id,
            reply_to_message_id=state.report.message_id,
            content="\n".join(lines),
        )
        result = await self._write(state, self.apps.discord.reply, intent)
        return state.advanced(state.step, discord_reply_id=result.remote_id)

    async def _attach_pr(self, state: RunState) -> RunState:
        """Attach the pull request to the issue of record."""
        await self._write(
            state,
            self.apps.linear.attach_url,
            self._intent(
                state,
                "linear.attach_url",
                target=str(state.linear_issue_id),
                issue_id=state.linear_issue_id,
                url=state.pr_url,
                title=f"Pull request #{state.pr_number}",
            ),
        )
        return state

    async def _verify_consistency(self, state: RunState, stop: Stop) -> RunState:
        """Read Discord, GitHub, and Linear back and record whether they agree."""
        consistency = await verify_step.assert_consistent(
            outcome=stop.outcome,
            run_id=state.run_id,
            github=self.apps.github,
            linear=self.apps.linear,
            discord=self.apps.discord,
            pr_number=state.pr_number,
            linear_issue_id=state.linear_issue_id,
            discord_message_id=state.discord_reply_id,
            discord_channel_id=state.report.channel_id,
            expect_ready=state.pr_ready,
        )
        self.ledger.append(
            state.run_id,
            "four_way_verified",
            {
                "consistent": consistency.consistent,
                "checks": consistency.checks,
                "problems": consistency.problems,
            },
            step=Step.VERIFY,
        )
        return state.advanced(
            state.step,
            consistent=consistency.consistent,
            consistency_problems=consistency.problems,
        )

    async def _publish_receipt(self, state: RunState, anchor: str) -> RunState:
        """Record the receipt where a reviewer can check it.

        `anchor` is fixed by the caller (the `run_stopped` event's own chain
        hash), never recomputed here. A value computed fresh on every call
        drifts with the chain position: a retry after a lost response, or a
        resumed replay of this same method, would each compute a different
        head hash and post the receipt a second time under a different value,
        because idempotency is matched by content and the two "attempts" would
        no longer describe the same write.
        """
        if state.linear_issue_id is None:
            return state
        await self._write(
            state,
            self.apps.linear.comment,
            self._intent(
                state,
                "linear.comment",
                # Its own idempotency key. On a duplicate the run has already
                # commented on this issue, and sharing that key made the ledger
                # treat the receipt as written when it never was.
                target=f"{state.linear_issue_id}:receipt",
                issue_id=state.linear_issue_id,
                body=(
                    f"Receipt `sha256:{anchor[:32]}`\n\n"
                    f"Every step of this run is hash chained. Verify it with "
                    f"`proofpr verify-receipt {state.run_id}`."
                ),
            ),
        )
        return state.advanced(state.step, receipt=anchor)

    # -- helpers --------------------------------------------------------------

    def _intent(
        self,
        state: RunState,
        operation: str,
        *,
        target: str,
        **payload: Any,  # noqa: ANN401 - payload shape differs per operation
    ) -> WriteIntent:
        """Build a write intent bound to this run."""
        return WriteIntent(
            run_id=state.run_id,
            app=operation.split(".", 1)[0],
            operation=operation,
            target=target,
            payload=payload,
        )

    async def _write(self, state: RunState, method: Any, intent: WriteIntent) -> WriteResult:  # noqa: ANN401
        """Perform a write exactly once, read it back, and record both.

        Three cases, in order:

        - Verified in the ledger: done, nothing is sent.
        - Attempted but never verified (a crash or a lost response): ask the
          application whether it already holds the write before sending again.
        - Never attempted: record the attempt, then send it.

        The attempt is recorded before sending so that a crash in between still
        leaves a trace that makes resume look before it writes.
        """
        previous = self.ledger.find_write(intent)
        if previous and previous["verified"]:
            self.ledger.append(
                state.run_id, "write_skipped", {"operation": intent.operation}, step=state.step
            )
            return WriteResult(
                intent=intent, remote_id=str(previous["remote_id"]), url=previous["url"]
            ).confirm({})

        adapter = getattr(self.apps, intent.app)
        result: WriteResult | None = None
        if previous is not None:
            result = await adapter.find_existing(intent)
            if result is not None:
                self._recovered(state, intent, result, after="an earlier attempt")
        if result is None:
            self.ledger.record_attempt(intent)
            result = await self._send(state, adapter, method, intent)

        self.ledger.record_write(result)
        self.ledger.append(
            state.run_id,
            "write_attempted",
            {"operation": intent.operation, "remote_id": result.remote_id},
            step=state.step,
        )

        adapter = getattr(self.apps, intent.app)
        verified: WriteResult = await adapter.readback(result)
        self.ledger.record_write(verified)
        self.ledger.append(
            state.run_id,
            "write_verified",
            {"operation": intent.operation, "observed": verified.observed},
            step=state.step,
        )
        return verified

    async def _send(
        self,
        state: RunState,
        adapter: Any,  # noqa: ANN401 - any application port
        method: Any,  # noqa: ANN401
        intent: WriteIntent,
    ) -> WriteResult:
        """Send a write, retrying transient failures without ever writing twice.

        A timeout or a 5xx says nothing about whether the change landed. Before
        each retry the application is asked; a write it already holds is taken
        as done. If the application cannot even be asked, the error propagates
        and the run pauses, because re-sending into an unknown state is the one
        move that can duplicate.

        Raises:
            RetryableAppError: Every attempt failed transiently.
        """
        attempts = max(1, self.config.write_attempts)
        for attempt in range(1, attempts + 1):
            try:
                result: WriteResult = await method(intent)
            except RetryableAppError as error:
                self.ledger.append(
                    state.run_id,
                    "write_retry",
                    {
                        "operation": intent.operation,
                        "attempt": attempt,
                        "status": error.status,
                        "error": str(error)[:300],
                    },
                    step=state.step,
                )
                found: WriteResult | None = await adapter.find_existing(intent)
                if found is not None:
                    self._recovered(state, intent, found, after="a lost response")
                    return found
                if attempt == attempts:
                    raise
                delay = self.config.write_backoff_seconds * 2 ** (attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                return result
        raise AssertionError("unreachable: the loop returns or raises")

    def _recovered(
        self, state: RunState, intent: WriteIntent, result: WriteResult, *, after: str
    ) -> None:
        """Record that a write was found already applied rather than sent again."""
        self.ledger.append(
            state.run_id,
            "write_recovered",
            {"operation": intent.operation, "remote_id": result.remote_id, "after": after},
            step=state.step,
        )
        logger.info("write_recovered", operation=intent.operation, after=after)

    @staticmethod
    def _fingerprint(state: RunState) -> fp.Fingerprint:
        """Rebuild the fingerprint from persisted state."""
        return fp.Fingerprint(
            exception=state.exception,
            path=state.path,
            function=state.function,
            terms=fp.extract_terms(state.report.text),
        )

    @staticmethod
    def _issue_title(state: RunState) -> str:
        """Build a title that names the defect rather than quoting the reporter."""
        if state.exception and state.function:
            return f"{state.exception} in {state.function}"
        if state.exception:
            return f"{state.exception} reported in {state.report.source}"
        first_line = state.report.text.strip().splitlines()[0]
        return first_line[:100]

    @staticmethod
    def _issue_body(state: RunState, stop: Stop) -> str:
        """Build the issue body, quoting the report inside a fenced block."""
        parts = [
            f"**Outcome:** `{stop.outcome.value}`"
            + (f" (`{stop.reason.value}`)" if stop.reason else ""),
            f"**Decided:** {stop.message}",
            "",
            "**Report**",
            "```",
            state.report.text.strip()[:4000],
            "```",
            "",
            f"- Source: {state.report.url or state.report.source}",
            f"- Reporter: {state.report.author or 'unknown'}",
            f"- Version reported: {state.reported_version or 'not stated'}",
            f"- Fingerprint: `{state.fingerprint_digest}`"
            + (f" ({state.exception} in {state.path}:{state.function})" if state.exception else ""),
        ]
        if state.repro_outcome:
            parts.append(
                f"- Reproduction: `{state.repro_outcome}`"
                + (f" from {state.repro_origin}" if state.repro_origin else "")
            )
        if state.repro_traceback:
            parts += [
                "",
                "**Captured traceback** (from running the reported input in the sandbox)",
                "```",
                state.repro_traceback[-2000:].strip(),
                "```",
            ]
        if state.proof_block:
            parts += ["", state.proof_block]
        if state.patch_summary:
            parts.append(f"- Proposed fix: {state.patch_summary} (`{state.patched_path}`)")
        if state.test_code:
            parts += [
                "",
                f"**Failing test** (`{state.test_path}`), verified to fail for the right reason "
                "on an otherwise green suite",
                "```python",
                state.test_code.strip(),
                "```",
            ]
        if state.injection_flags:
            parts.append(
                f"- Injection flags recorded: {', '.join(state.injection_flags)}. "
                "The text above was treated as data only."
            )
        return "\n".join(parts)


def _stop_pr_opened(state: RunState) -> None:
    """End a run that published a proof-carrying pull request."""
    raise Stop(
        Outcome.PR_OPENED,
        None,
        f"Opened a proof-carrying pull request: {state.pr_url}. {state.ci_summary}",
        state,
    )


def _stop_triage_only(state: RunState) -> None:
    """End a run that survived every triage stage.

    Separate from the pipeline body so the terminal state is raised from one
    place, and so the message is easy to find when M3 replaces it with
    reproduction.
    """
    raise Stop(
        Outcome.TRIAGED,
        None,
        "Reproduced, fixed, and proved. Filed for a human with the proof attached: "
        "this build stops before opening a pull request.",
        state,
    )


_SKIPPED_BY_BASELINE = frozenset({Step.EXISTS, Step.WORTH_IT})

#: Maps an exists finding to the terminal state and the message the reporter sees.
_EXISTS_OUTCOMES: dict[str, Callable[[ExistsFinding], tuple[Outcome, Reason | None, str]]] = {
    "duplicate": lambda finding: (
        Outcome.DUPLICATE,
        None,
        f"Already tracked as {finding.reference}. Added you to it.",
    ),
    "already_fixed": lambda finding: (
        Outcome.ALREADY_FIXED,
        None,
        f"This was fixed after the version you are running: {finding.evidence}. "
        "Please upgrade and reopen if it persists.",
    ),
    "fix_in_flight": lambda finding: (
        Outcome.FIX_IN_FLIGHT,
        None,
        f"A fix is already open: {finding.reference}.",
    ),
}
