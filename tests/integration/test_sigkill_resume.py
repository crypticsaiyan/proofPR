"""A real process death, not a simulated one.

The unit tests raise from inside a step, which is what a crash looks like to the
ledger. This one sends SIGKILL to a real process mid-run and then inspects the
database from a different process, which is what a crash looks like to SQLite:
no unwinding, no flush, no chance to write anything on the way down.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from proofpr.domain.enums import Step
from proofpr.ledger import Ledger
from tests.conftest import REPO_ROOT

pytestmark = [pytest.mark.slow]

CHILD = """
import sys, time
from pathlib import Path

sys.path.insert(0, {src!r})

from proofpr.domain.enums import Step
from proofpr.domain.run import Report, RunState
from proofpr.ledger import Ledger

ledger = Ledger(Path({db!r}))
run_id = ledger.start_run(source="sigkill-test", run_id="r-kill")

state = RunState(run_id=run_id, report=Report(source="sigkill-test", text="a crash report"))
ledger.append(run_id, "step_started", step=Step.SANITIZE)
ledger.append(run_id, "step_finished", state.snapshot(), step=Step.SANITIZE)
ledger.append(run_id, "step_started", step=Step.PRE_CHECK)

# Tell the parent we are at the point worth killing, then hang.
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, str]:
    """Return the ledger path and the child program."""
    database = tmp_path / "ledger.db"
    program = CHILD.format(src=str(REPO_ROOT / "src"), db=str(database))
    return database, program


def run_and_kill(program: str) -> int:
    """Start the child, wait until it is mid-run, and kill it outright.

    The pipe is closed explicitly rather than left to the garbage collector,
    which otherwise surfaces as an unraisable exception during a later test.
    """
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        return int(child.returncode)
    finally:
        if child.stdout is not None:
            child.stdout.close()
        if child.poll() is None:  # pragma: no cover - only on an unexpected hang
            child.kill()
            child.wait(timeout=5)


def test_a_killed_process_leaves_a_resumable_ledger(paths: tuple[Path, str]) -> None:
    database, program = paths

    returncode = run_and_kill(program)

    assert returncode == -signal.SIGKILL

    # A different process, opening the database after the writer was killed
    # outright. Nothing was flushed on the way down, because nothing had the
    # chance to run.
    ledger = Ledger(database)
    try:
        unfinished = ledger.unfinished_runs()
        assert [row["run_id"] for row in unfinished] == ["r-kill"]

        # The step that was in flight is identified, the completed one is not.
        assert ledger.resume_point("r-kill") is Step.PRE_CHECK

        # Every event written before the kill survived, and the chain still
        # verifies: WAL plus synchronous=FULL is the reason this holds.
        intact, broken_at = ledger.verify_chain("r-kill")
        assert intact, f"chain broken at {broken_at}"
        assert [event["kind"] for event in ledger.events("r-kill")] == [
            "run_started",
            "step_started",
            "step_finished",
            "step_started",
        ]

        # A killed run has no receipt, and says so rather than verifying vacuously.
        verified, detail = ledger.verify_receipt("r-kill")
        assert verified is False
        assert "never reached a terminal state" in detail
    finally:
        ledger.close()


def test_the_database_is_usable_immediately_after_a_kill(paths: tuple[Path, str]) -> None:
    database, program = paths
    run_and_kill(program)

    # No recovery pause, no lock left behind: the next writer just works.
    started = time.monotonic()
    ledger = Ledger(database)
    try:
        ledger.append("r-kill", "resumed", {"by": "the test"})
        assert ledger.verify_chain("r-kill")[0] is True
    finally:
        ledger.close()
    assert time.monotonic() - started < 5
