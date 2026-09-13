"""Logging is configurable, idempotent, and binds run identifiers to every line."""

from __future__ import annotations

import json

import pytest

from proofpr.observability.logging import bind_run, clear_run, configure_logging, get_logger


def _emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Parse the JSON lines written to stderr by the renderer."""
    return [json.loads(line) for line in capsys.readouterr().err.strip().splitlines() if line]


def test_configure_is_idempotent() -> None:
    configure_logging(level="DEBUG", json_output=True)
    configure_logging(level="INFO", json_output=True)

    assert get_logger("proofpr.test") is not None


def test_json_renderer_emits_parseable_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=True)
    get_logger("proofpr.test").info("write_verified", app="github")

    payload = _emitted(capsys)[-1]
    assert payload["event"] == "write_verified"
    assert payload["app"] == "github"
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_run_identifiers_appear_on_every_line_until_cleared(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", json_output=True)

    bind_run("r-7f3a", trace_id="abc123")
    get_logger("proofpr.test").info("step_started", step="pre_check")
    clear_run()
    get_logger("proofpr.test").info("unbound")

    bound, unbound = _emitted(capsys)[-2:]
    assert bound["run_id"] == "r-7f3a"
    assert bound["trace_id"] == "abc123"
    assert bound["step"] == "pre_check"
    assert "run_id" not in unbound


def test_level_filtering_drops_quieter_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", json_output=True)
    get_logger("proofpr.test").info("dropped")
    get_logger("proofpr.test").warning("kept")

    events = [line["event"] for line in _emitted(capsys)]
    assert "dropped" not in events
    assert "kept" in events
