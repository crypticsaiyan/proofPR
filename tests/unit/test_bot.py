"""The Discord bot's pure rendering logic.

The gateway client itself (`ProofPRBot`) is not exercised here: it needs a
real or heavily mocked `discord.Client`, and everything it decides is decided
by these functions, which is what is tested directly.
"""

from __future__ import annotations

from proofpr.bot import _approval_components, _outcome_text, _phase_lines, _status_text
from proofpr.domain.enums import Outcome, Reason, Step
from proofpr.domain.run import Report, RunState


def _event(kind: str, step: Step, **payload: object) -> dict[str, object]:
    return {"kind": kind, "step": step.value, "payload": payload}


class TestPhaseLines:
    """The ✅/⏳ block, built from ledger events alone."""

    def test_no_events_is_no_lines(self) -> None:
        assert _phase_lines([]) == []

    def test_a_started_step_shows_in_progress(self) -> None:
        lines = _phase_lines([_event("step_started", Step.SANITIZE)])
        assert lines == ["⏳ Intake"]

    def test_a_finished_step_shows_done(self) -> None:
        events = [_event("step_started", Step.PATCH), _event("step_finished", Step.PATCH)]
        assert _phase_lines(events) == ["✅ Patch + proof"]

    def test_a_phase_with_two_steps_waits_for_both(self) -> None:
        # Intake spans SANITIZE and PRE_CHECK; only one finishing keeps it ⏳.
        events = [
            _event("step_started", Step.SANITIZE),
            _event("step_finished", Step.SANITIZE),
            _event("step_started", Step.PRE_CHECK),
        ]
        assert _phase_lines(events) == ["⏳ Intake"]

    def test_a_skipped_step_counts_as_finished(self) -> None:
        events = [_event("step_started", Step.PATCH), _event("step_skipped", Step.PATCH)]
        assert _phase_lines(events) == ["✅ Patch + proof"]

    def test_phases_appear_in_declared_order_not_event_order(self) -> None:
        events = [
            _event("step_started", Step.PUBLISH),
            _event("step_started", Step.SANITIZE),
        ]
        assert _phase_lines(events) == ["⏳ Intake", "⏳ Publish"]

    def test_a_row_ticks_when_the_steps_that_started_have_finished(self) -> None:
        # Clarify never started, so it must not hold the row open.
        events = [_event("step_started", Step.REPRO_RAW), _event("step_finished", Step.REPRO_RAW)]
        assert _phase_lines(events) == ["✅ Reproduce + test"]

    def test_a_failed_ci_marks_the_ci_row_failed(self) -> None:
        events = [
            _event("step_started", Step.CI_WAIT),
            _event("step_finished", Step.CI_WAIT),
            _event("run_stopped", Step.CI_WAIT, outcome="ci_failed"),
        ]
        assert _phase_lines(events) == ["❌ CI"]

    def test_a_deliberate_stop_is_not_shown_as_a_failure(self) -> None:
        events = [
            _event("step_started", Step.EXISTS),
            _event("step_finished", Step.EXISTS),
            _event("run_stopped", Step.EXISTS, outcome="duplicate"),
        ]
        assert _phase_lines(events) == ["⏹️ Exists"]

    def test_an_untouched_phase_is_omitted(self) -> None:
        lines = _phase_lines([_event("step_started", Step.PATCH)])
        assert lines == ["⏳ Patch + proof"]


class TestStatusText:
    """The whole status message header plus phase block."""

    def test_carries_the_run_id_cost_and_phases(self) -> None:
        text = _status_text(
            "r-abc123",
            [_event("step_started", Step.SANITIZE)],
            cost_usd=0.06,
            started=0.0,
        )
        assert "r-abc123" in text
        assert "$0.06" in text
        assert "⏳ Intake" in text


class TestOutcomeText:
    """The terminal line appended once a run concludes."""

    def _state(self, **overrides: object) -> RunState:
        report = Report(source="discord", text="it crashes")
        return RunState(run_id="r-1", report=report, **overrides)

    def test_bare_outcome(self) -> None:
        state = self._state(outcome=Outcome.NOT_A_BUG)
        assert "not_a_bug" in _outcome_text(state)

    def test_outcome_with_reason(self) -> None:
        state = self._state(outcome=Outcome.OUT_OF_SCOPE, reason=Reason.OUT_OF_FIX_CLASS)
        text = _outcome_text(state)
        assert "out_of_scope" in text
        assert "out_of_fix_class" in text

    def test_pr_opened_carries_the_link(self) -> None:
        state = self._state(outcome=Outcome.PR_OPENED, pr_url="https://github.com/x/y/pull/1")
        assert "https://github.com/x/y/pull/1" in _outcome_text(state)

    def test_proof_complete_carries_mutant_count(self) -> None:
        state = self._state(
            outcome=Outcome.PR_OPENED, proof_complete=True, mutants_killed=9, mutants_total=9
        )
        assert "9/9" in _outcome_text(state)


class TestInjectionFlagsAreShown:
    """A report that tried to give orders says so in the result."""

    def test_flags_appear_in_the_outcome(self) -> None:
        state = RunState(
            run_id="r-1",
            report=Report(source="discord", text="x"),
            outcome=Outcome.PR_OPENED,
            injection_flags=["privilege_request"],
        )
        assert "privilege_request" in _outcome_text(state)


class TestApprovalComponents:
    """The raw Discord button payload."""

    def test_two_buttons_carry_the_run_id(self) -> None:
        [row] = _approval_components("r-42")
        custom_ids = [button["custom_id"] for button in row["components"]]
        assert custom_ids == ["proofpr:approve:r-42", "proofpr:reject:r-42"]
