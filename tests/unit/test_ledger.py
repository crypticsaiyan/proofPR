"""The ledger resumes, verifies, and refuses to forget."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from proofpr.domain.enums import Arm, Outcome, Reason, Step
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.ledger import GENESIS_HASH, Ledger


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """An open ledger over a temporary file, migrated from the real SQL."""
    return Ledger(tmp_path / "ledger.db")


def test_migrations_apply_once_and_are_idempotent(ledger: Ledger) -> None:
    assert ledger.migrate() == []


def test_wal_and_full_durability_are_actually_set(ledger: Ledger) -> None:
    connection = sqlite3.connect(ledger.path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()


def test_a_run_records_its_own_start_and_finish(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="discord", arm=Arm.PROOFPR)

    receipt = ledger.finish_run(run_id, outcome=Outcome.DUPLICATE)

    row = ledger.get_run(run_id)
    assert row is not None
    assert row["outcome"] == "duplicate"
    assert row["receipt"] == receipt
    assert len(receipt) == 64


def test_the_chain_starts_at_genesis_and_advances(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")

    assert ledger.head_hash("unknown-run") == GENESIS_HASH
    first = ledger.head_hash(run_id)
    ledger.append(run_id, "step_started", {"step": "pre_check"}, step=Step.PRE_CHECK)
    assert ledger.head_hash(run_id) != first


def test_verify_chain_detects_an_edited_event(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")
    ledger.append(run_id, "gate_result", {"passed": False}, step=Step.GATE)
    assert ledger.verify_chain(run_id) == (True, None)

    connection = sqlite3.connect(ledger.path)
    connection.execute(
        "UPDATE events SET payload = ? WHERE kind = 'gate_result'", ('{"passed":true}',)
    )
    connection.commit()
    connection.close()

    intact, seq = ledger.verify_chain(run_id)
    assert intact is False
    assert seq is not None


def test_resume_point_names_the_step_a_crash_landed_in(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")
    ledger.append(run_id, "step_started", step=Step.PRE_CHECK)
    ledger.append(run_id, "step_finished", step=Step.PRE_CHECK)
    ledger.append(run_id, "step_started", step=Step.REPRO_RAW)

    assert ledger.resume_point(run_id) is Step.REPRO_RAW


def test_a_run_with_no_unfinished_step_has_no_resume_point(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")
    ledger.append(run_id, "step_started", step=Step.EXISTS)
    ledger.append(run_id, "step_finished", step=Step.EXISTS)

    assert ledger.resume_point(run_id) is None


def test_unfinished_runs_are_listed_for_resume(ledger: Ledger) -> None:
    stuck = ledger.start_run(source="discord")
    done = ledger.start_run(source="discord")
    ledger.finish_run(done, outcome=Outcome.NOT_A_BUG, reason=Reason.NOT_A_BUG_REPORT)

    assert [row["run_id"] for row in ledger.unfinished_runs()] == [stuck]


def test_a_write_is_recorded_once_per_operation_and_target(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")
    intent = WriteIntent(
        run_id=run_id,
        app="linear",
        operation="linear.create_issue",
        target="TEAM",
        payload={"title": "x"},
    )
    result = WriteResult(intent=intent, remote_id="iss-1")

    ledger.record_write(result)
    ledger.record_write(result.confirm({"identifier": "ENG-1"}))

    found = ledger.find_write(intent)
    assert found is not None
    assert found["verified"] == 1
    assert found["remote_id"] == "iss-1"


def test_a_retry_finds_its_own_earlier_write_before_touching_the_app(ledger: Ledger) -> None:
    run_id = ledger.start_run(source="cli")
    intent = WriteIntent(
        run_id=run_id, app="github", operation="github.open_pr", target="repo", payload={}
    )
    assert ledger.find_write(intent) is None

    ledger.record_write(WriteResult(intent=intent, remote_id="42"))

    assert ledger.find_write(intent) is not None


def test_cost_is_summed_per_run_and_overall(ledger: Ledger) -> None:
    first = ledger.start_run(source="cli")
    second = ledger.start_run(source="cli")
    ledger.record_model_call(
        first,
        step=Step.PRE_CHECK,
        tier="cheap",
        model="haiku",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.01,
    )
    ledger.record_model_call(
        second,
        step=Step.TEST_SYNTH,
        tier="strong",
        model="opus",
        input_tokens=900,
        output_tokens=400,
        cost_usd=0.20,
    )

    assert ledger.total_cost(first) == pytest.approx(0.01)
    assert ledger.total_cost() == pytest.approx(0.21)


def test_the_ledger_survives_being_reopened(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger(path) as first:
        run_id = first.start_run(source="cli")
        first.append(run_id, "step_started", step=Step.GATE)
        head = first.head_hash(run_id)

    with Ledger(path) as second:
        assert second.head_hash(run_id) == head
        assert second.resume_point(run_id) is Step.GATE
        assert second.verify_chain(run_id) == (True, None)
