"""The evaluation datasets must stay loadable and internally consistent.

A dataset that stops parsing, or whose ground truth contradicts itself, produces
numbers that look fine and mean nothing. That is a build failure here.
"""

from __future__ import annotations

import sys

import pytest

from proofpr.domain.enums import Outcome, Reason
from tests.conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "eval"))

from schema import Case

SEEDED = REPO_ROOT / "eval" / "datasets" / "seeded"


@pytest.fixture(scope="module")
def cases() -> list[Case]:
    """Every seeded case."""
    loaded: list[Case] = Case.load_all(SEEDED)
    return loaded


def test_the_seeded_dataset_loads(cases: list[Case]) -> None:
    assert cases


def test_every_expected_outcome_is_a_real_terminal_state(cases: list[Case]) -> None:
    outcomes = {outcome.value for outcome in Outcome}

    for case in cases:
        assert case.expected_outcome in outcomes, f"{case.id}: {case.expected_outcome}"


def test_every_expected_reason_is_a_real_reason_code(cases: list[Case]) -> None:
    reasons = {reason.value for reason in Reason}

    for case in cases:
        if case.expected_reason is not None:
            assert case.expected_reason in reasons, f"{case.id}: {case.expected_reason}"


def test_ground_truth_does_not_contradict_itself(cases: list[Case]) -> None:
    for case in cases:
        if case.expected_outcome in {"triaged", "pr_opened"}:
            assert case.in_scope, f"{case.id} expects work but is marked out of scope"
        if case.expected_outcome == "out_of_scope":
            assert not case.in_scope, f"{case.id} expects out_of_scope but is marked in scope"
        if case.duplicate_of:
            assert case.expected_outcome == "duplicate", f"{case.id} names a duplicate target"


def test_case_identifiers_are_unique_and_prefixed(cases: list[Case]) -> None:
    identifiers = [case.id for case in cases]

    assert len(identifiers) == len(set(identifiers))
    assert all(identifier.startswith("seeded-") for identifier in identifiers)
