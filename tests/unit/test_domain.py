"""The domain vocabulary is a persisted wire format, so it is pinned by tests."""

from __future__ import annotations

import pytest

from proofpr.domain.enums import (
    CODE_WRITING_OUTCOMES,
    NO_GATE_SKIPPED_STEPS,
    Arm,
    Outcome,
    Reason,
    Step,
)
from proofpr.domain.errors import (
    GuardBlockedError,
    PermanentAppError,
    ProofPRError,
    RetryableAppError,
)


def test_enum_values_are_stable_snake_case() -> None:
    for enum in (Step, Outcome, Reason, Arm):
        for member in enum:
            assert member.value == member.value.lower()
            assert " " not in member.value
            assert member.value.replace("_", "").isalnum()


def test_pipeline_order_runs_cheap_checks_before_the_sandbox() -> None:
    order = list(Step)

    assert order.index(Step.PRE_CHECK) < order.index(Step.EXISTS)
    assert order.index(Step.EXISTS) < order.index(Step.WORTH_IT)
    assert order.index(Step.WORTH_IT) < order.index(Step.REPRO_RAW)
    assert order.index(Step.REPRO_RAW) < order.index(Step.TEST_SYNTH)
    assert order.index(Step.TEST_SYNTH) < order.index(Step.GATE)
    assert order.index(Step.GATE) < order.index(Step.PATCH)


def test_baseline_arm_skips_every_proof_bearing_step() -> None:
    assert Step.GATE in NO_GATE_SKIPPED_STEPS
    assert Step.PROOF in NO_GATE_SKIPPED_STEPS
    assert Step.REPRO_RAW in NO_GATE_SKIPPED_STEPS
    # The baseline still localizes and patches; that is the point of the comparison.
    assert Step.LOCALIZE not in NO_GATE_SKIPPED_STEPS
    assert Step.PATCH not in NO_GATE_SKIPPED_STEPS


def test_only_code_writing_outcomes_are_counted_as_such() -> None:
    assert Outcome.PR_OPENED in CODE_WRITING_OUTCOMES
    for outcome in (
        Outcome.DUPLICATE,
        Outcome.ALREADY_FIXED,
        Outcome.OUT_OF_SCOPE,
        Outcome.NOT_A_BUG,
        Outcome.DECLINED,
        Outcome.CONFIRMED_NO_TEST,
    ):
        assert outcome not in CODE_WRITING_OUTCOMES


def test_errors_carry_stable_codes() -> None:
    assert ProofPRError("boom").code == "proofpr_error"
    assert "[retryable_app_error]" in str(RetryableAppError("429", app="github", status=429))
    assert PermanentAppError("401", app="linear", status=401).app == "linear"


def test_guard_blocked_records_the_operation_and_the_rule() -> None:
    error = GuardBlockedError(
        "collaborator changes are never allowed",
        operation="github.add_collaborator",
        rule="tighten_only",
    )

    assert error.operation == "github.add_collaborator"
    assert error.rule == "tighten_only"
    assert isinstance(error, ProofPRError)


def test_retryable_and_permanent_do_not_share_a_code() -> None:
    with pytest.raises(RetryableAppError) as excinfo:
        raise RetryableAppError("timeout", app="linear")
    assert excinfo.value.code != PermanentAppError("x", app="linear").code
