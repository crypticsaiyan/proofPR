"""Branch names, commit subjects, and the pull request body."""

from __future__ import annotations

import pytest

from proofpr.steps.publish import branch_name, commit_subject, pr_body, pr_title


class TestBranch:
    """A branch traces back to the run that made it."""

    def test_the_run_id_is_in_the_name(self) -> None:
        assert branch_name("r-7f3a") == "proofpr/r-7f3a"

    def test_the_prefix_is_configurable_because_the_allowlist_enforces_it(self) -> None:
        assert branch_name("r-7f3a", "bots/proofpr/") == "bots/proofpr/r-7f3a"


class TestCommitSubject:
    """Conventional Commits, and short enough to read in a log."""

    def test_a_summary_becomes_a_fix_subject(self) -> None:
        assert commit_subject("Validate the month", "TypeError") == "fix: validate the month"

    def test_a_trailing_period_is_dropped(self) -> None:
        assert commit_subject("validate the month.", None) == "fix: validate the month"

    def test_an_empty_summary_falls_back_to_the_exception(self) -> None:
        assert "TypeError" in commit_subject("", "TypeError")

    def test_a_long_summary_is_truncated(self) -> None:
        assert len(commit_subject("x" * 200, None)) <= 72


class TestTitle:
    """The title names the defect, not the reporter."""

    def test_the_summary_is_used_when_there_is_one(self) -> None:
        assert pr_title("validate the month", "TypeError", "parse_date") == (
            "fix: validate the month"
        )

    def test_without_a_summary_it_names_the_exception_and_function(self) -> None:
        assert pr_title("", "TypeError", "parse_date") == "fix: handle TypeError in parse_date"

    def test_with_nothing_at_all_it_still_says_something_true(self) -> None:
        assert pr_title("", None, None) == "fix: handle invalid input"


class TestBody:
    """The proof leads, because the proof is the part a reviewer can check."""

    def body(self, **overrides: object) -> str:
        """Render a body with sensible defaults."""
        arguments: dict[str, object] = {
            "proof_block": "## Proof\n\n| Check | Result |\n|---|---|\n| x | y |",
            "rationale": "The month is not validated before the length lookup.",
            "report_url": "https://discord.com/channels/1/2/3",
            "linear_url": "https://linear.app/x/ENG-42",
            "linear_identifier": "ENG-42",
            "run_id": "r-7f3a",
        }
        arguments.update(overrides)
        return pr_body(**arguments)  # type: ignore[arg-type]

    def test_the_proof_block_comes_first(self) -> None:
        assert self.body().startswith("## Proof")

    def test_every_input_is_linked(self) -> None:
        body = self.body()

        assert "https://discord.com/channels/1/2/3" in body
        assert "ENG-42" in body

    def test_the_run_marker_is_present_for_idempotent_retry(self) -> None:
        assert "proofpr-run:r-7f3a" in self.body()

    def test_missing_links_are_simply_absent(self) -> None:
        body = self.body(report_url=None, linear_url=None, linear_identifier=None)

        assert "## Inputs" not in body
        assert "proofpr-run:r-7f3a" in body

    @pytest.mark.parametrize("field", ["rationale"])
    def test_an_empty_section_is_omitted_rather_than_left_blank(self, field: str) -> None:
        assert "## Why this is the cause" not in self.body(**{field: ""})
