"""The CLI exposes the documented surface and fails loudly on unbuilt commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from proofpr import __version__
from proofpr.cli import app
from tests.conftest import REPO_ROOT

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_every_documented_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("doctor", "run", "eval", "report", "reconcile", "verify-receipt"):
        assert command in result.stdout


def test_doctor_reports_configuration_and_exits_nonzero_until_m1() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "ProofPR configuration" in result.stdout


def test_unbuilt_commands_never_exit_zero() -> None:
    # Reconcile is built now; the evaluation harness and the report generator
    # land in M8 and still say so rather than pretending.
    for argv in (["eval"], ["report"]):
        assert runner.invoke(app, argv).exit_code == 1


def test_reconcile_reports_rather_than_raising_without_credentials(tmp_path: Path) -> None:
    result = runner.invoke(app, ["reconcile"], env={"PROOFPR_DB_PATH": str(tmp_path / "l.db")})

    assert result.exit_code == 0
    assert "checked 0" in result.stdout


def test_verify_receipt_fails_loudly_on_an_unknown_run(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["verify-receipt", "r-nope"], env={"PROOFPR_DB_PATH": str(tmp_path / "l.db")}
    )

    assert result.exit_code == 1
    assert "not verified" in result.stdout


def test_resume_says_so_when_there_is_nothing_to_resume(tmp_path: Path) -> None:
    result = runner.invoke(app, ["resume"], env={"PROOFPR_DB_PATH": str(tmp_path / "l.db")})

    assert result.exit_code == 0
    assert "no unfinished runs" in result.stdout


def test_a_fixture_run_against_in_memory_apps_succeeds() -> None:
    result = runner.invoke(
        app,
        ["run", str(REPO_ROOT / "eval" / "datasets" / "seeded" / "004-chatter.yaml"), "--dry-run"],
    )

    assert result.exit_code == 0
    assert "not_a_bug" in result.stdout
