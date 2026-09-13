"""The receipt: what it covers, and what it detects."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from proofpr.domain.enums import Outcome, Step
from proofpr.ledger import GENESIS_HASH, Ledger


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """An open ledger."""
    return Ledger(tmp_path / "ledger.db")


def receipt_of(ledger: Ledger, run_id: str) -> str:
    """Return a finished run's receipt, asserting the run exists."""
    row = ledger.get_run(run_id)
    assert row is not None
    return str(row["receipt"])


def finished_run(ledger: Ledger) -> str:
    """Create a small finished run."""
    run_id = ledger.start_run(source="test")
    ledger.append(run_id, "step_started", step=Step.SANITIZE)
    ledger.append(run_id, "step_finished", {"ok": True}, step=Step.SANITIZE)
    ledger.finish_run(run_id, outcome=Outcome.TRIAGED)
    return run_id


class TestWhatItCovers:
    """The receipt covers everything up to the closing event, and is inside it."""

    def test_a_finished_run_verifies(self, ledger: Ledger) -> None:
        run_id = finished_run(ledger)

        verified, detail = ledger.verify_receipt(run_id)

        assert verified is True
        assert "verify against receipt" in detail

    def test_the_receipt_is_the_hash_before_the_closing_event(self, ledger: Ledger) -> None:
        # This is what makes it printable: a digest covering the event that
        # announces it could never appear in the pull request or the issue.
        run_id = finished_run(ledger)
        events = ledger.events(run_id)

        assert receipt_of(ledger, run_id) == events[-2]["chain_hash"]

    def test_the_closing_event_carries_the_same_receipt(self, ledger: Ledger) -> None:
        run_id = finished_run(ledger)
        events = ledger.events(run_id)

        assert events[-1]["payload"]["receipt"] == receipt_of(ledger, run_id)

    def test_two_runs_have_different_receipts(self, ledger: Ledger) -> None:
        first = receipt_of(ledger, finished_run(ledger))
        second = receipt_of(ledger, finished_run(ledger))

        assert first != second


class TestWhatItDetects:
    """Any edit to a recorded step is detectable by recomputation."""

    def test_an_edited_payload_breaks_verification(self, ledger: Ledger) -> None:
        run_id = finished_run(ledger)

        connection = sqlite3.connect(ledger.path)
        connection.execute(
            "UPDATE events SET payload = ? WHERE kind = 'step_finished'", ('{"ok":false}',)
        )
        connection.commit()
        connection.close()

        verified, detail = ledger.verify_receipt(run_id)
        assert verified is False
        assert "chain is broken" in detail

    def test_a_forged_receipt_is_detected(self, ledger: Ledger) -> None:
        run_id = finished_run(ledger)

        connection = sqlite3.connect(ledger.path)
        connection.execute("UPDATE runs SET receipt = ? WHERE run_id = ?", ("0" * 64, run_id))
        connection.commit()
        connection.close()

        verified, detail = ledger.verify_receipt(run_id)
        assert verified is False
        assert "does not match" in detail

    def test_a_deleted_event_breaks_verification(self, ledger: Ledger) -> None:
        run_id = finished_run(ledger)

        connection = sqlite3.connect(ledger.path)
        connection.execute(
            "DELETE FROM events WHERE kind = 'step_started' AND run_id = ?", (run_id,)
        )
        connection.commit()
        connection.close()

        assert ledger.verify_receipt(run_id)[0] is False

    def test_an_appended_event_after_sealing_breaks_verification(self, ledger: Ledger) -> None:
        # Appending after the close is itself detectable: the receipt no longer
        # sits one event from the end.
        run_id = finished_run(ledger)

        ledger.append(run_id, "sneaky", {"added": "later"})

        verified, detail = ledger.verify_receipt(run_id)
        assert verified is False
        assert "no closing event" in detail


class TestHonestFailures:
    """Unverifiable is reported as unverifiable, never as verified."""

    def test_an_unknown_run_does_not_verify(self, ledger: Ledger) -> None:
        verified, detail = ledger.verify_receipt("r-nope")

        assert verified is False
        assert "no run" in detail

    def test_an_unfinished_run_says_it_never_finished(self, ledger: Ledger) -> None:
        run_id = ledger.start_run(source="test")
        ledger.append(run_id, "step_started", step=Step.SANITIZE)

        verified, detail = ledger.verify_receipt(run_id)

        assert verified is False
        assert "never reached a terminal state" in detail

    def test_an_empty_chain_starts_at_the_published_genesis(self, ledger: Ledger) -> None:
        assert ledger.head_hash("r-never-existed") == GENESIS_HASH


class TestAnEarlierCheckpointReceipt:
    """A receipt published mid-run, before the closing event, still verifies.

    `finish_run` accepts an explicit receipt so a run that already told a
    reviewer a number can seal with that exact number, rather than a fresh one
    computed after later events (the publishing write itself, the final state
    snapshot) extended the chain further. Verification checks that the
    published value is *some* real position in the intact chain, not only the
    one immediately before the closing event.
    """

    def test_a_receipt_from_an_earlier_event_verifies(self, ledger: Ledger) -> None:
        run_id = ledger.start_run(source="test")
        checkpoint = ledger.append(run_id, "step_started", step=Step.SANITIZE)
        ledger.append(run_id, "step_finished", {"ok": True}, step=Step.SANITIZE)
        ledger.append(run_id, "step_started", step=Step.PRE_CHECK)

        sealed = ledger.finish_run(run_id, outcome=Outcome.TRIAGED, receipt=checkpoint)

        assert sealed == checkpoint
        verified, detail = ledger.verify_receipt(run_id)
        assert verified is True, detail

    def test_a_receipt_matching_nothing_in_the_chain_is_forged(self, ledger: Ledger) -> None:
        run_id = ledger.start_run(source="test")
        ledger.append(run_id, "step_started", step=Step.SANITIZE)

        ledger.finish_run(run_id, outcome=Outcome.TRIAGED, receipt="f" * 64)

        verified, detail = ledger.verify_receipt(run_id)
        assert verified is False
        assert "does not match" in detail

    def test_omitting_the_receipt_falls_back_to_the_chain_head(self, ledger: Ledger) -> None:
        run_id = ledger.start_run(source="test")
        ledger.append(run_id, "step_started", step=Step.SANITIZE)
        last = ledger.append(run_id, "step_finished", {"ok": True}, step=Step.SANITIZE)

        sealed = ledger.finish_run(run_id, outcome=Outcome.TRIAGED)

        assert sealed == last

    def test_an_edit_before_the_checkpoint_still_breaks_verification(self, ledger: Ledger) -> None:
        # The relaxation must not let an attacker rewrite history that precedes
        # the published checkpoint: the chain integrity check still runs first.
        run_id = ledger.start_run(source="test")
        checkpoint = ledger.append(run_id, "step_started", step=Step.SANITIZE)
        ledger.append(run_id, "step_finished", {"ok": True}, step=Step.SANITIZE)

        connection = sqlite3.connect(ledger.path)
        connection.execute(
            "UPDATE events SET payload = ? WHERE kind = 'step_started'", ('{"tampered":true}',)
        )
        connection.commit()
        connection.close()

        ledger.finish_run(run_id, outcome=Outcome.TRIAGED, receipt=checkpoint)

        verified, detail = ledger.verify_receipt(run_id)
        assert verified is False
        assert "chain is broken" in detail
