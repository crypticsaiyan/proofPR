"""Extracting something runnable from a report, deterministically."""

from __future__ import annotations

import pytest

from proofpr.triage import repro_input

TRACEBACK_REPORT = """\
parse_date blows up on month 13.

Traceback (most recent call last):
  File "app.py", line 12, in <module>
    parse_date("2024-13-01")
  File "src/validkit/dates.py", line 42, in parse_date
    if day > length:
TypeError: '>' not supported between instances of 'int' and 'NoneType'
"""


class TestFromReport:
    """Three forms, in order of how much the reporter committed to."""

    def test_a_fenced_block_wins(self) -> None:
        text = "here:\n```python\nparse_date('2024-13-01')\n```\n" + TRACEBACK_REPORT

        result = repro_input.from_report(text)

        assert result is not None
        assert result.origin == "fenced_block"
        assert result.symbol == "parse_date"

    def test_repl_prompts_are_stripped_from_a_pasted_session(self) -> None:
        text = "```\n>>> parse_date('2024-13-01')\n```"

        result = repro_input.from_report(text)

        assert result is not None
        assert result.code == "parse_date('2024-13-01')"

    def test_the_traceback_call_line_is_used_when_there_is_no_block(self) -> None:
        result = repro_input.from_report(TRACEBACK_REPORT)

        assert result is not None
        assert result.origin == "traceback_call"
        assert result.code == 'parse_date("2024-13-01")'

    def test_only_the_reporters_own_frame_is_used(self) -> None:
        # `if day > length:` is a library frame's source line, not an input.
        result = repro_input.from_report(TRACEBACK_REPORT)

        assert result is not None
        assert "day > length" not in result.code

    def test_an_inline_call_with_an_argument_is_accepted(self) -> None:
        result = repro_input.from_report("calling parse_date('2024-13-01') blows up on me")

        assert result is not None
        assert result.origin == "inline_call"

    def test_a_bare_function_mention_is_not_input(self) -> None:
        # "parse_date() is broken" describes a symbol, not an input, and running
        # it would reproduce a different error entirely.
        assert repro_input.from_report("parse_date() is broken for some months") is None

    def test_a_report_with_nothing_runnable_returns_none(self) -> None:
        assert repro_input.from_report("dates are broken, please fix") is None

    def test_unparseable_code_is_not_offered_as_input(self) -> None:
        assert repro_input.from_report("```python\nparse_date(((\n```") is None


class TestScript:
    """The generated script says nothing on success beyond its exit code."""

    def test_an_expression_is_evaluated(self) -> None:
        repro = repro_input.ReproInput(code="parse_date('x')", origin="test")

        script = repro_input.build_script(repro, package="validkit")

        assert "import validkit" in script
        assert "result = parse_date('x')" in script

    def test_a_block_is_executed_as_written(self) -> None:
        repro = repro_input.ReproInput(code="a = 1\nparse_date(str(a))", origin="test")

        script = repro_input.build_script(repro, package="validkit")

        assert "a = 1\nparse_date(str(a))" in script

    @pytest.mark.parametrize("code", ["parse_date('x')", "a = 1\nprint(a)"])
    def test_every_generated_script_parses(self, code: str) -> None:
        script = repro_input.build_script(
            repro_input.ReproInput(code=code, origin="test"), package="validkit"
        )

        assert repro_input.parses(script)
