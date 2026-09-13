"""The datasets, the splits, the arms, and the runner's bookkeeping.

Nothing here starts a container. What is checked is the part of the harness that
decides what a number means: that the splits are frozen, complete and disjoint,
that every case carries ground truth, that the baseline arm is defined by the
checks it skips, and that a patch nobody judged is reported as unjudged.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from arms import ARMS, BASELINE, PROOFPR, SKIPPED_BY_BASELINE, describe_difference
from proofpr.domain.enums import NO_GATE_SKIPPED_STEPS, Arm
from proofpr.domain.errors import ConfigurationError
from proofpr.evaluation import load_cases, load_split
from proofpr.steps.patch import ProposedPatch
from proofpr.steps.test_synth import SynthesizedTest
from proofpr.triage.intent import IntentVerdict
from runner import CaseResult, Runner, prepare_repo
from schema import Case
from scripted import ScriptedModel
from tests.conftest import EVAL_DIR, REPO_ROOT

DATASETS = EVAL_DIR / "datasets"


def all_cases() -> list[Case]:
    """Every case in every dataset."""
    cases: list[Case] = []
    for directory in sorted(DATASETS.iterdir()):
        if directory.is_dir():
            cases += Case.load_all(directory)
    return cases


class TestArms:
    """The baseline is a code path, not a strawman."""

    def test_both_arms_are_registered_under_their_labels(self) -> None:
        assert ARMS["proofpr"] is PROOFPR
        assert ARMS["no_gate"] is BASELINE

    def test_only_our_arm_carries_proof(self) -> None:
        assert PROOFPR.writes_proof is True
        assert BASELINE.writes_proof is False

    def test_the_skipped_steps_match_the_pipeline(self) -> None:
        # If these ever drift, the report would name checks the baseline still
        # runs, or stay silent about ones it does not.
        assert set(SKIPPED_BY_BASELINE) == set(NO_GATE_SKIPPED_STEPS)

    def test_the_difference_names_every_skipped_check(self) -> None:
        rendered = describe_difference()

        for step in SKIPPED_BY_BASELINE:
            assert step.value in rendered

    def test_the_arms_map_to_distinct_ledger_arms(self) -> None:
        assert PROOFPR.arm is Arm.PROOFPR
        assert BASELINE.arm is Arm.NO_GATE


class TestDatasets:
    """A case is data plus ground truth, or it is not a case."""

    def test_every_case_loads_and_declares_an_expected_outcome(self) -> None:
        cases = all_cases()

        assert len(cases) >= 20
        assert all(case.expected_outcome for case in cases)

    def test_case_identifiers_are_unique_across_datasets(self) -> None:
        identifiers = [case.id for case in all_cases()]

        assert len(identifiers) == len(set(identifiers))

    def test_an_unknown_field_is_a_load_error(self, tmp_path: Path) -> None:
        # Ground truth typed under a misspelled key would silently default, and
        # the case would measure something other than what it claims to.
        path = tmp_path / "bad.yaml"
        path.write_text(
            textwrap.dedent(
                """
                id: bad-001
                text: hello
                expected_outcome: declined
                reproducable: true
                """
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="reproducable"):
            Case.load(path)

    def test_every_injection_case_carries_a_real_bug_underneath(self) -> None:
        # An injection case that expected a decline would pass by declining
        # everything. Each one hides a genuine, reproducible bug, so the only
        # way to score it is to fix the bug and refuse the instruction.
        cases = Case.load_all(DATASETS / "injection")

        assert len(cases) >= 10
        assert all(case.expected_outcome == "pr_opened" for case in cases)
        assert all(case.reproducible is True for case in cases)

    def test_every_code_writing_case_has_a_hidden_test_to_judge_it(self) -> None:
        for dataset in ("seeded", "injection"):
            hidden = {path.stem for path in (DATASETS / dataset / "hidden").glob("*.py")}
            publishable = {
                case.id
                for case in Case.load_all(DATASETS / dataset)
                if case.expected_outcome == "pr_opened"
            }
            assert publishable, dataset
            assert publishable <= hidden, dataset

    def test_no_case_that_should_stop_early_carries_a_hidden_test(self) -> None:
        # A hidden test for a case that should never produce a patch would be
        # dead weight, and would suggest a patch was expected.
        hidden = {path.stem for path in (DATASETS / "seeded" / "hidden").glob("*.py")}
        early = {
            case.id
            for case in Case.load_all(DATASETS / "seeded")
            if case.expected_outcome != "pr_opened"
        }

        assert hidden & early == set()


class TestSplits:
    """Frozen, complete, disjoint."""

    def test_dev_and_test_are_disjoint(self) -> None:
        dev = set(load_split("dev", eval_dir=EVAL_DIR).case_ids)
        test = set(load_split("test", eval_dir=EVAL_DIR).case_ids)

        assert dev & test == set()

    def test_every_case_is_in_exactly_one_split(self) -> None:
        assigned = set(load_split("dev", eval_dir=EVAL_DIR).case_ids) | set(
            load_split("test", eval_dir=EVAL_DIR).case_ids
        )
        existing = {case.id for case in all_cases()}

        assert assigned == existing

    def test_the_split_records_when_it_was_frozen(self) -> None:
        assert load_split("dev", eval_dir=EVAL_DIR).frozen_at

    def test_both_splits_are_stratified_by_expected_outcome(self) -> None:
        # Stratification is the reason the headline metric is measurable on the
        # test half at all: a split with every publishable case on one side
        # would leave the other unable to price the checks.
        for name in ("dev", "test"):
            cases = load_cases(load_split(name, eval_dir=EVAL_DIR), eval_dir=EVAL_DIR)
            outcomes = {case.expected_outcome for case in cases}
            assert "pr_opened" in outcomes, name
            assert len(outcomes) >= 2, name

    def test_loading_the_named_cases_preserves_the_split_order(self) -> None:
        split = load_split("test", eval_dir=EVAL_DIR)

        cases = load_cases(split, eval_dir=EVAL_DIR)

        assert [case.id for case in cases] == list(split.case_ids)

    def test_an_unknown_split_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="no split named"):
            load_split("nope", eval_dir=EVAL_DIR)

    def test_a_missing_harness_is_a_configuration_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="no splits file"):
            load_split("dev", eval_dir=tmp_path)

    def test_a_split_naming_a_case_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "splits.yaml").write_text("dev:\n  - ghost-999\n", encoding="utf-8")
        (tmp_path / "runner.py").write_text("", encoding="utf-8")
        (tmp_path / "datasets").mkdir()

        with pytest.raises(ConfigurationError, match="do not exist"):
            load_cases(load_split("dev", eval_dir=tmp_path), eval_dir=tmp_path)


class TestCaseResult:
    """What counts as unsafe, and what refuses to count either way."""

    def make(self, **overrides: Any) -> CaseResult:
        fields: dict[str, Any] = {
            "case_id": "c1",
            "arm": "no_gate",
            "run_id": "r-1",
            "outcome": "pr_opened",
            "reason": None,
            "expected_outcome": "pr_opened",
            "cost_usd": 0.0,
            "wrote_code": True,
        }
        fields.update(overrides)
        return CaseResult(**fields)

    def test_a_rejected_patch_is_unsafe(self) -> None:
        assert self.make(hidden_test_passed=False).unsafe is True

    def test_a_patch_that_passes_is_not_unsafe(self) -> None:
        assert self.make(hidden_test_passed=True).unsafe is False

    def test_an_unjudged_patch_is_not_counted_as_safe_or_unsafe(self) -> None:
        result = self.make(hidden_test_passed=None)

        assert result.unsafe is False
        assert result.hidden_test_passed is None

    def test_a_run_that_wrote_nothing_cannot_be_unsafe(self) -> None:
        assert self.make(wrote_code=False, hidden_test_passed=False).unsafe is False

    def test_an_outcome_is_correct_only_when_it_matches_ground_truth(self) -> None:
        assert self.make().outcome_correct is True
        assert self.make(outcome="declined").outcome_correct is False


class TestHiddenTests:
    """Held out means held out."""

    def runner(self, tmp_path: Path, datasets_root: Path | None) -> Runner:
        return Runner(
            settings=None,
            ledger=None,
            repo_path=tmp_path,
            package="validkit",
            model_factory=lambda _case: None,
            datasets_root=datasets_root,
        )

    def test_a_case_with_a_hidden_test_finds_it(self, tmp_path: Path) -> None:
        runner = self.runner(tmp_path, DATASETS)
        case = next(c for c in Case.load_all(DATASETS / "seeded") if c.id == "seeded-001")

        assert "def test_" in (runner._hidden_test(case) or "")

    def test_a_case_in_another_dataset_is_found_too(self, tmp_path: Path) -> None:
        # A split mixes datasets. Searching only one would report half the
        # patches as unjudged and quietly shrink the denominator.
        runner = self.runner(tmp_path, DATASETS)
        case = next(c for c in Case.load_all(DATASETS / "injection") if c.id == "injection-001")

        assert "def test_" in (runner._hidden_test(case) or "")

    def test_a_case_without_one_returns_none(self, tmp_path: Path) -> None:
        runner = self.runner(tmp_path, DATASETS)
        case = next(c for c in Case.load_all(DATASETS / "seeded") if c.id == "seeded-004")

        assert runner._hidden_test(case) is None

    async def test_a_case_with_no_hidden_test_is_reported_as_unjudged(self, tmp_path: Path) -> None:
        runner = self.runner(tmp_path, None)
        case = Case(id="x-1", text="hi", expected_outcome="declined")

        passed, detail = await runner._apply_hidden_test(case, "src/x.py", "x = 1")

        assert passed is None
        assert "no hidden test" in detail

    async def test_a_run_with_no_patch_is_reported_as_unjudged(self, tmp_path: Path) -> None:
        runner = self.runner(tmp_path, DATASETS)
        case = next(c for c in Case.load_all(DATASETS / "seeded") if c.id == "seeded-001")

        passed, detail = await runner._apply_hidden_test(case, None, None)

        assert passed is None
        assert "no patch" in detail

    def test_no_hidden_test_lives_inside_the_target_repository(self) -> None:
        # The agent gets a worktree of this repository. A hidden test committed
        # into it would be one `cat` away from the prompt.
        target = REPO_ROOT / "tests" / "fixtures" / "validkit"

        assert not list(target.rglob("*hidden*"))


class TestPrepareRepo:
    """The evaluation patches a copy, never the checkout."""

    def test_the_copy_is_complete_and_separate(self, tmp_path: Path) -> None:
        source = tmp_path / "src_repo"
        (source / "src").mkdir(parents=True)
        (source / "src" / "a.py").write_text("x = 1", encoding="utf-8")

        copy = prepare_repo(source, tmp_path / "copy")
        (copy / "src" / "a.py").write_text("x = 2", encoding="utf-8")

        assert (source / "src" / "a.py").read_text() == "x = 1"

    def test_an_existing_destination_is_replaced(self, tmp_path: Path) -> None:
        source = tmp_path / "src_repo"
        source.mkdir()
        (source / "new.py").write_text("", encoding="utf-8")
        destination = tmp_path / "copy"
        destination.mkdir()
        (destination / "stale.py").write_text("", encoding="utf-8")

        prepare_repo(source, destination)

        assert not (destination / "stale.py").exists()
        assert (destination / "new.py").exists()


class TestScriptedModel:
    """The stand-in used when there are no credentials."""

    def case(self, text: str, **overrides: Any) -> Case:
        fields: dict[str, Any] = {"id": "x-1", "text": text, "expected_outcome": "pr_opened"}
        fields.update(overrides)
        return Case(**fields)

    def test_the_module_comes_from_the_traceback(self) -> None:
        model = ScriptedModel(self.case('File "src/validkit/numbers.py", line 20, in parse_port'))

        assert model.module == "numbers"
        assert model.source_path == "src/validkit/numbers.py"

    def test_a_case_with_no_traceback_falls_back_rather_than_crashing(self) -> None:
        assert ScriptedModel(self.case("dates are broken")).module == "dates"

    async def test_the_intent_answer_is_the_ground_truth(self) -> None:
        model = ScriptedModel(self.case("anything", intent="question"))

        verdict, _ = await model.complete_json(system="", user="", schema=IntentVerdict)

        assert verdict.intent == "question"

    async def test_the_patch_actually_fixes_the_fixture(self) -> None:
        # A scripted patch that did not fix the bug would make every harness run
        # end in an unsafe pull request and prove nothing about the harness.
        model = ScriptedModel(self.case('File "src/validkit/dates.py", line 30, in parse_date'))

        patch, _ = await model.complete_json(system="", user="", schema=ProposedPatch)

        buggy = REPO_ROOT / "tests" / "fixtures" / "validkit" / "src" / "validkit" / "dates.py"
        assert patch.files[0].path == "src/validkit/dates.py"
        assert patch.files[0].content != buggy.read_text()
        assert "month" in patch.files[0].content

    async def test_the_test_it_writes_lands_under_tests(self) -> None:
        model = ScriptedModel(self.case('File "src/validkit/text.py", line 20, in slugify'))

        test, _ = await model.complete_json(system="", user="", schema=SynthesizedTest)

        assert test.path.startswith("tests/")
        assert "slugify" in test.code

    async def test_every_call_is_free_and_labelled(self) -> None:
        model = ScriptedModel(self.case("anything"))

        _, usage = await model.complete_json(system="", user="", schema=IntentVerdict)

        assert usage.cost_usd == 0.0
        assert usage.model == "scripted-stub"
        assert usage.prompt_version == "scripted-stub"

    async def test_an_unfamiliar_schema_is_filled_rather_than_raised(self) -> None:
        from pydantic import BaseModel

        class Question(BaseModel):
            question: str

        answer, _ = await ScriptedModel(self.case("anything")).complete_json(
            system="", user="", schema=Question
        )

        assert answer.question
