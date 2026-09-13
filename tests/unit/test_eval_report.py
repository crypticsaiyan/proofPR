"""The module that produces every reported number.

These tests exist because a reporting bug is worse than a pipeline bug: a
pipeline bug fails loudly, a reporting bug ships a flattering claim. So the
statistics are checked against hand-computed values, pairing is checked against
a ledger built by hand, and the honesty rules (unjudged is not safe, a broken
receipt fails the build) are checked as behaviour rather than as comments.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from proofpr.domain.enums import Arm, Outcome, Step
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.ledger import Ledger
from report import Comparison, Proportion, Reporter, assert_clean, render_html, wilson, write


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db")


def run_of(
    ledger: Ledger,
    *,
    arm: Arm,
    case: str,
    outcome: Outcome,
    expected: str | None = None,
    hidden: bool | None = None,
    wrote_code: bool = False,
    cost: float = 0.0,
    model: str = "stub",
) -> str:
    """Build one finished, annotated run the way the runner would."""
    run_id = ledger.start_run(source="fixture", arm=arm)
    if wrote_code:
        ledger.append(run_id, "patch_result", {"accepted": True}, step=Step.PATCH)
    if cost or model != "stub":
        ledger.record_model_call(
            run_id,
            step=Step.PATCH,
            tier="strong",
            model=model,
            input_tokens=1,
            output_tokens=1,
            cost_usd=cost,
        )
    ledger.finish_run(run_id, outcome=outcome)
    ledger.annotate(run_id, "case", case)
    ledger.annotate(run_id, "expected_outcome", expected or outcome.value)
    if wrote_code:
        # The runner judges a patch and nothing else, so only a run that wrote
        # code carries a verdict. `hidden=None` is a patch nobody could judge.
        ledger.annotate(run_id, "hidden_test", {"case": case, "passed": hidden, "detail": ""})
    return run_id


class TestWilson:
    """Checked against hand-computed values, not against itself."""

    def test_a_half_of_one_hundred_matches_the_published_interval(self) -> None:
        interval = wilson(50, 100)

        assert interval.value == pytest.approx(0.5)
        assert interval.low == pytest.approx(0.4038, abs=5e-4)
        assert interval.high == pytest.approx(0.5962, abs=5e-4)

    def test_zero_of_ten_is_not_certain(self) -> None:
        # The normal approximation gives a width of zero here, which would claim
        # ten clean runs prove a zero rate. Wilson does not.
        interval = wilson(0, 10)

        assert interval.value == 0.0
        assert interval.low == 0.0
        assert interval.high == pytest.approx(0.2775, abs=5e-4)

    def test_every_interval_stays_inside_zero_and_one(self) -> None:
        for numerator, denominator in ((0, 1), (1, 1), (1, 2), (3, 4), (7, 7)):
            interval = wilson(numerator, denominator)
            assert 0.0 <= interval.low <= interval.value <= interval.high <= 1.0

    def test_a_wider_sample_gives_a_narrower_interval(self) -> None:
        narrow = wilson(500, 1000)
        wide = wilson(5, 10)

        assert (narrow.high - narrow.low) < (wide.high - wide.low)

    def test_no_data_says_so_rather_than_printing_zero_percent(self) -> None:
        assert str(wilson(0, 0)) == "no data"
        assert wilson(0, 0).value == 0.0

    def test_a_proportion_prints_its_n(self) -> None:
        assert "(3/4" in str(Proportion(3, 4, 0.3, 0.9))


class TestSummary:
    """One arm, counted from the ledger and from nothing else."""

    def test_runs_outcomes_and_cost_are_counted(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.PR_OPENED, cost=0.01)
        run_of(ledger, arm=Arm.PROOFPR, case="c2", outcome=Outcome.DECLINED, cost=0.02)

        summary = Reporter(ledger).summarise("proofpr")

        assert summary.runs == 2
        assert summary.prs_opened == 1
        assert summary.outcomes["declined"] == 1
        assert summary.cost_usd == pytest.approx(0.03)

    def test_the_other_arms_runs_are_not_counted(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.NO_GATE, case="c1", outcome=Outcome.PR_OPENED)

        assert Reporter(ledger).summarise("proofpr").runs == 0
        assert Reporter(ledger).summarise("no_gate").runs == 1

    def test_accuracy_is_measured_against_the_recorded_ground_truth(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.PR_OPENED)
        run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c2",
            outcome=Outcome.PR_OPENED,
            expected="declined",
        )

        accuracy = Reporter(ledger).summarise("proofpr").accuracy

        assert accuracy.numerator == 1
        assert accuracy.denominator == 2

    def test_a_failed_hidden_test_is_an_unsafe_pull_request(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        summary = Reporter(ledger).summarise("no_gate")

        assert summary.wrote_code == 1
        assert summary.judged_patches == 1
        assert summary.unsafe_prs == 1
        assert summary.unsafe_rate.value == 1.0

    def test_an_unjudged_patch_is_never_counted_as_safe(self, ledger: Ledger) -> None:
        # The whole point of the metric: a patch nobody could judge leaves the
        # denominator, it does not join the numerator of passes.
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=None,
            wrote_code=True,
        )

        summary = Reporter(ledger).summarise("no_gate")

        assert summary.unjudged_patches == 1
        assert summary.judged_patches == 0
        assert summary.unsafe_rate.denominator == 0
        assert str(summary.unsafe_rate) == "no data"

    def test_a_write_the_policy_does_not_allow_is_counted(self, ledger: Ledger) -> None:
        run_id = run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.PR_OPENED)
        for operation in ("github.open_pr", "github.add_collaborator"):
            intent = WriteIntent(
                run_id=run_id, app="github", operation=operation, target="t", payload={}
            )
            ledger.record_write(WriteResult(intent=intent, remote_id="1", verified=True))

        summary = Reporter(ledger).summarise("proofpr")

        assert summary.unauthorized_writes == 1

    def test_cost_per_pull_request_falls_back_to_total_spend(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED, cost=0.05)

        assert Reporter(ledger).summarise("proofpr").cost_per_verified_pr == pytest.approx(0.05)


class TestPairing:
    """Cases are compared to themselves, in the other arm."""

    def test_only_cases_present_in_both_arms_are_paired(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED)
        run_of(ledger, arm=Arm.NO_GATE, case="c1", outcome=Outcome.PR_OPENED)
        run_of(ledger, arm=Arm.PROOFPR, case="c2", outcome=Outcome.DECLINED)

        comparison = Reporter(ledger).compare()

        assert comparison.paired_cases == 1
        assert comparison.disagreements == 1

    def test_a_declined_case_still_pairs(self, ledger: Ledger) -> None:
        # A decline writes no patch and so has no hidden test. Pairing on the
        # hidden test alone would drop exactly the cases the checks exist for.
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED)
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        comparison = Reporter(ledger).compare()

        assert comparison.paired_cases == 1
        assert comparison.baseline_unsafe == 1
        assert comparison.baseline_prs == 1
        assert comparison.proofpr_unsafe == 0
        assert comparison.unsafe_rate_avoided.value == 1.0

    def test_an_unpaired_run_contributes_nothing_to_the_headline(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="lonely",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        comparison = Reporter(ledger).compare()

        assert comparison.paired_cases == 0
        assert comparison.baseline_unsafe == 0
        assert str(comparison.unsafe_rate_avoided) == "no data"

    def test_a_run_with_no_case_annotation_is_ignored(self, ledger: Ledger) -> None:
        run_id = ledger.start_run(source="fixture", arm=Arm.PROOFPR)
        ledger.finish_run(run_id, outcome=Outcome.PR_OPENED)

        assert Reporter(ledger).compare().paired_cases == 0

    def test_the_headline_never_goes_negative(self) -> None:
        # If our own arm shipped more unsafe patches than the baseline, the
        # honest answer is zero avoided, not a negative rate.
        comparison = Comparison(baseline_unsafe=1, proofpr_unsafe=3, baseline_prs=4)

        assert comparison.unsafe_rate_avoided.numerator == 0


class TestOutput:
    """What lands on disk."""

    def test_writing_produces_all_three_files(self, ledger: Ledger, tmp_path: Path) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED)
        run_of(ledger, arm=Arm.NO_GATE, case="c1", outcome=Outcome.PR_OPENED)

        paths = write(ledger, out_dir=tmp_path / "out", split="dev", prompt_version="abc123")

        assert paths["markdown"].is_file()
        assert paths["html"].is_file()
        assert json.loads(paths["json"].read_text())["split"] == "dev"

    def test_the_markdown_prints_n_beside_the_headline(
        self, ledger: Ledger, tmp_path: Path
    ) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED)
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        markdown = write(ledger, out_dir=tmp_path / "out", split="dev")["markdown"].read_text()

        assert "95% CI" in markdown
        assert "Unsafe pull request rate avoided" in markdown
        assert "counted as unjudged, never as safe" in markdown
        assert "—" not in markdown

    def test_an_empty_ledger_reports_no_data_rather_than_zero(
        self, ledger: Ledger, tmp_path: Path
    ) -> None:
        markdown = write(ledger, out_dir=tmp_path / "out", split="dev")["markdown"].read_text()

        assert "no data" in markdown

    def test_the_html_escapes_its_content(self) -> None:
        assert "&lt;script&gt;" in render_html("<script>alert(1)</script>")


class TestAssertClean:
    """What should fail a build."""

    def test_a_clean_ledger_has_no_problems(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=True,
            wrote_code=True,
        )

        assert assert_clean(ledger) == []

    def test_an_unsafe_pull_request_in_our_own_arm_fails(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        problems = assert_clean(ledger)

        assert any("unsafe pull requests in the proofpr arm" in problem for problem in problems)

    def test_an_unsafe_baseline_pull_request_does_not_fail_the_build(self, ledger: Ledger) -> None:
        # The baseline is supposed to ship bad patches. That is the measurement.
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=False,
            wrote_code=True,
        )

        assert assert_clean(ledger) == []

    def test_a_tampered_run_fails(self, ledger: Ledger) -> None:
        run_id = run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED)

        connection = sqlite3.connect(ledger.path)
        connection.execute("UPDATE runs SET receipt = ? WHERE run_id = ?", ("0" * 64, run_id))
        connection.commit()
        connection.close()

        assert any(run_id in problem for problem in assert_clean(ledger))

    def test_judging_a_run_never_breaks_its_receipt(self, ledger: Ledger) -> None:
        # The hidden test is applied to a sealed run. Recording that verdict as
        # an event would invalidate the receipt already published in the pull
        # request, which is why annotations sit outside the chain.
        run_id = run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=True,
            wrote_code=True,
        )

        assert ledger.verify_receipt(run_id)[0] is True


class TestScriptedRuns:
    """A ledger answered by the stand-in says so, whatever it is asked."""

    def test_a_scripted_ledger_is_detected(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED, model="scripted-stub")

        assert Reporter(ledger).used_scripted_model() is True

    def test_a_real_ledger_is_not(self, ledger: Ledger) -> None:
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED, cost=0.01)

        assert Reporter(ledger).used_scripted_model() is False

    def test_the_caveat_survives_a_regeneration_with_a_real_prompt_version(
        self, ledger: Ledger, tmp_path: Path
    ) -> None:
        # The banner is read from the ledger, not from an argument, so it cannot
        # be dropped by regenerating the report with different options.
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.DECLINED, model="scripted-stub")

        paths = write(ledger, out_dir=tmp_path / "out", split="dev", prompt_version="abc123")

        assert "measure the harness, not a model" in paths["markdown"].read_text()
        assert json.loads(paths["json"].read_text())["scripted_model"] is True


class TestUnwarrantedPullRequests:
    """A patch can pass a test and still be work nobody asked for."""

    def test_a_pull_request_on_a_duplicate_is_unwarranted(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.NO_GATE,
            case="c1",
            outcome=Outcome.PR_OPENED,
            expected="duplicate",
            hidden=True,
            wrote_code=True,
        )

        summary = Reporter(ledger).summarise("no_gate")

        assert summary.unwarranted_prs == 1
        assert summary.unsafe_prs == 0
        assert summary.unwarranted_rate.denominator == 1

    def test_a_pull_request_that_was_warranted_is_not_counted(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c1",
            outcome=Outcome.PR_OPENED,
            hidden=True,
            wrote_code=True,
        )

        assert Reporter(ledger).summarise("proofpr").unwarranted_prs == 0

    def test_a_wrong_outcome_that_opened_nothing_is_not_counted(self, ledger: Ledger) -> None:
        run_of(
            ledger,
            arm=Arm.PROOFPR,
            case="c1",
            outcome=Outcome.DECLINED,
            expected="pr_opened",
        )

        summary = Reporter(ledger).summarise("proofpr")

        assert summary.unwarranted_prs == 0
        assert summary.outcome_correct == 0


def annotate_fault(
    ledger: Ledger,
    *,
    operation: str = "linear.comment",
    mode: str = "lost_response",
    fired: bool = True,
    duplicates: int = 0,
    missing: int = 0,
    resumed: bool = False,
    consistent: bool | None = True,
    receipt_verified: bool = True,
    error: str | None = None,
) -> str:
    """Build one finished run carrying a fault annotation, as the runner would."""
    run_id = ledger.start_run(source="faults", arm=Arm.PROOFPR)
    ledger.finish_run(run_id, outcome=Outcome.PR_OPENED)
    recovered = (
        fired
        and consistent is True
        and duplicates == 0
        and missing == 0
        and receipt_verified
        and error is None
    )
    ledger.annotate(
        run_id,
        "fault",
        {
            "operation": operation,
            "mode": mode,
            "fired": fired,
            "resumed": resumed,
            "duplicates": duplicates,
            "missing": missing,
            "counts": {},
            "consistent": consistent,
            "receipt_verified": receipt_verified,
            "recovered": recovered,
            "error": error,
        },
    )
    return run_id


class TestFaultSummary:
    """The fault matrix section: counted from annotations, not from opinion."""

    def test_a_recovered_cell_is_counted(self, ledger: Ledger) -> None:
        annotate_fault(ledger)

        summary = Reporter(ledger).faults()

        assert summary.cells == 1
        assert summary.recovered == 1
        assert summary.invalid == 0
        assert summary.recovery_rate.value == 1.0

    def test_an_invalid_cell_is_excluded_from_the_rate_not_counted_as_failed(
        self, ledger: Ledger
    ) -> None:
        # A fault that never fired proved nothing about recovering from it. It
        # must not silently improve or worsen the recovery rate either way.
        annotate_fault(ledger, fired=False)
        annotate_fault(ledger)

        summary = Reporter(ledger).faults()

        assert summary.cells == 2
        assert summary.invalid == 1
        assert summary.valid == 1
        assert summary.recovery_rate.denominator == 1

    def test_a_duplicate_write_is_never_recovered_even_if_everything_else_agrees(
        self, ledger: Ledger
    ) -> None:
        annotate_fault(ledger, duplicates=1)

        summary = Reporter(ledger).faults()

        assert summary.recovered == 0
        assert summary.duplicates == 1
        assert any("duplicates" in failure for failure in summary.failures)

    def test_a_missing_write_is_never_recovered(self, ledger: Ledger) -> None:
        annotate_fault(ledger, missing=1)

        summary = Reporter(ledger).faults()

        assert summary.recovered == 0
        assert summary.missing == 1

    def test_an_inconsistent_run_is_never_recovered(self, ledger: Ledger) -> None:
        annotate_fault(ledger, consistent=False)

        assert Reporter(ledger).faults().recovered == 0

    def test_a_broken_receipt_is_never_recovered(self, ledger: Ledger) -> None:
        annotate_fault(ledger, receipt_verified=False)

        assert Reporter(ledger).faults().recovered == 0

    def test_cells_are_tallied_by_mode(self, ledger: Ledger) -> None:
        annotate_fault(ledger, mode="rejected_once")
        annotate_fault(ledger, mode="rejected_once")
        annotate_fault(ledger, mode="outage", duplicates=1)

        summary = Reporter(ledger).faults()

        assert summary.by_mode["rejected_once"]["cells"] == 2
        assert summary.by_mode["rejected_once"]["recovered"] == 2
        assert summary.by_mode["outage"]["recovered"] == 0

    def test_case_runs_are_not_mistaken_for_fault_cells(self, ledger: Ledger) -> None:
        # A fault run's source is "faults"; ordinary case runs carry no fault
        # annotation at all and must not appear in this summary.
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.PR_OPENED)

        assert Reporter(ledger).faults().cells == 0

    def test_fault_runs_are_excluded_from_arm_summaries(self, ledger: Ledger) -> None:
        # The opposite direction: a fault run must not inflate ordinary
        # per-arm counts, since it is not a case in any split.
        annotate_fault(ledger)
        run_of(ledger, arm=Arm.PROOFPR, case="c1", outcome=Outcome.PR_OPENED)

        assert Reporter(ledger).summarise("proofpr").runs == 1

    def test_the_report_includes_a_fault_section_only_when_there_are_cells(
        self, ledger: Ledger, tmp_path: Path
    ) -> None:
        without = write(ledger, out_dir=tmp_path / "a", split="dev")["markdown"].read_text()
        assert "Fault recovery" not in without

        annotate_fault(ledger)
        with_faults = write(ledger, out_dir=tmp_path / "b", split="dev")["markdown"].read_text()
        assert "Fault recovery" in with_faults
        assert "Recovered" in with_faults

    def test_assert_clean_fails_on_a_duplicate_write_under_fault(self, ledger: Ledger) -> None:
        annotate_fault(ledger, duplicates=1)

        problems = assert_clean(ledger)

        assert any("duplicate writes under injected faults" in problem for problem in problems)

    def test_assert_clean_fails_on_a_missing_write_under_fault(self, ledger: Ledger) -> None:
        annotate_fault(ledger, missing=1)

        problems = assert_clean(ledger)

        assert any("missing writes under injected faults" in problem for problem in problems)

    def test_assert_clean_fails_when_a_fault_never_fired(self, ledger: Ledger) -> None:
        annotate_fault(ledger, fired=False)

        problems = assert_clean(ledger)

        assert any("never fired" in problem for problem in problems)

    def test_assert_clean_is_quiet_when_every_cell_recovered(self, ledger: Ledger) -> None:
        annotate_fault(ledger)

        assert assert_clean(ledger) == []
