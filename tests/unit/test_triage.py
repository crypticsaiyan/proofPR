"""Triage rules: fingerprinting, versions, intent, and scope."""

from __future__ import annotations

import pytest

from proofpr.domain.enums import Reason
from proofpr.triage import fingerprint as fp
from proofpr.triage import intent as intent_rules
from proofpr.triage import scope_rules, version_range

TRACEBACK = """\
Traceback (most recent call last):
  File "app.py", line 12, in <module>
    parse_date("2024-13-01")
  File "/home/u/.venv/lib/python3.12/site-packages/validkit/dates.py", line 42, in parse_date
    return _build(year, month, day)
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
"""


class TestFingerprint:
    """A crash gets a deterministic identity from its structure, not its prose."""

    def test_the_innermost_frame_is_the_one_that_counts(self) -> None:
        result = fp.from_text(TRACEBACK)

        assert result.exception == "TypeError"
        assert result.path == "validkit/dates.py"
        assert result.function == "parse_date"

    def test_site_packages_prefixes_are_normalised_away(self) -> None:
        assert fp.normalise_path("/x/.venv/lib/python3.12/site-packages/validkit/a.py") == (
            "validkit/a.py"
        )
        assert fp.normalise_path("./src/validkit/a.py") == "validkit/a.py"

    def test_the_digest_ignores_prose_entirely(self) -> None:
        first = fp.from_text(TRACEBACK)
        second = fp.from_text(f"totally different words\n{TRACEBACK}")

        assert first.digest == second.digest

    def test_a_report_with_no_structure_is_not_structural(self) -> None:
        assert fp.from_text("it broke again").is_structural is False

    def test_paraphrased_duplicates_score_highly(self) -> None:
        first = fp.from_text(TRACEBACK)
        second = fp.from_text(TRACEBACK.replace("blows up", "explodes"))

        assert first.similarity(second) > 0.85

    def test_two_different_exceptions_in_one_file_do_not_look_the_same(self) -> None:
        first = fp.from_text(TRACEBACK)
        second = fp.from_text(TRACEBACK.replace("TypeError", "ValueError"))

        assert first.similarity(second) < 0.7

    def test_search_terms_are_most_specific_first(self) -> None:
        terms = fp.search_terms(fp.from_text(TRACEBACK))

        assert terms[:3] == ["TypeError", "parse_date", "dates.py"]


class TestVersionRange:
    """Versions compare the way a support window needs them to."""

    @pytest.mark.parametrize(
        ("lower", "higher"),
        [
            ("1.2.3", "1.2.4"),
            ("1.9", "1.10"),
            ("2.0.0dev1", "2.0.0a1"),
            ("2.0.0a1", "2.0.0b1"),
            ("2.0.0rc1", "2.0.0"),
        ],
    )
    def test_ordering(self, lower: str, higher: str) -> None:
        parsed_lower, parsed_higher = version_range.parse(lower), version_range.parse(higher)
        assert parsed_lower is not None
        assert parsed_higher is not None
        assert parsed_lower < parsed_higher

    def test_trailing_zeros_do_not_change_a_version(self) -> None:
        assert version_range.parse("1.4") == version_range.parse("1.4.0")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("running version 0.4.1", "0.4.1"),
            ("on v1.2.3 here", "1.2.3"),
            ("I use 2.0.0rc1", "2.0.0rc1"),
            ("no version mentioned at all", None),
            ("issue 13 in module 4", None),
        ],
    )
    def test_finding_a_version_in_a_report(self, text: str, expected: str | None) -> None:
        found = version_range.find_in_text(text)

        assert (str(found) if found else None) == expected

    def test_specifiers(self) -> None:
        version = version_range.parse("0.4.1")
        assert version is not None

        assert version_range.satisfies(version, ">=0.3.0")
        assert version_range.satisfies(version, ">=0.3.0,<1.0.0")
        assert not version_range.satisfies(version, ">=1.0.0")

    def test_an_unparseable_specifier_raises_rather_than_widening_the_window(self) -> None:
        version = version_range.parse("1.0.0")
        assert version is not None

        with pytest.raises(ValueError, match="unparseable"):
            version_range.satisfies(version, "~=1.0")


class TestIntent:
    """Rules stop only what they are certain about, and evidence always wins."""

    @pytest.mark.parametrize("text", ["thanks!", "hi", "ok", "+1", "good morning"])
    def test_greetings_are_chatter(self, text: str) -> None:
        result = intent_rules.rule_check(text)

        assert result is not None
        assert result.intent is intent_rules.Intent.CHATTER
        assert result.should_continue is False

    def test_a_traceback_always_wins(self) -> None:
        result = intent_rules.rule_check("thanks! " + TRACEBACK)

        assert result is not None
        assert result.intent is intent_rules.Intent.BUG
        assert result.decided_by == "rule:evidence"

    def test_an_exception_name_alone_counts_as_evidence(self) -> None:
        result = intent_rules.rule_check("I get a ValueError when the month is 13")

        assert result is not None
        assert result.has_error_output is True

    def test_an_ordinary_report_is_left_to_the_model(self) -> None:
        assert intent_rules.rule_check("the validator accepts month 13 and it should not") is None

    def test_a_low_confidence_non_bug_verdict_still_continues(self) -> None:
        verdict = intent_rules.IntentVerdict(
            intent=intent_rules.Intent.QUESTION, confidence=0.4, rationale="unclear"
        )

        assert intent_rules.from_verdict(verdict, "text").should_continue is True

    def test_a_confident_non_bug_verdict_stops(self) -> None:
        verdict = intent_rules.IntentVerdict(
            intent=intent_rules.Intent.FEATURE, confidence=0.9, rationale="asks for new behaviour"
        )

        assert intent_rules.from_verdict(verdict, "text").should_continue is False


class TestScope:
    """Four rules decide whether code may be written, and nothing else does."""

    def make(self, **overrides: object) -> scope_rules.ScopeDecision:
        """Run the scope decision with sensible defaults."""
        arguments = {
            "fingerprint": fp.from_text(TRACEBACK),
            "reported_version": "0.4.1",
            "supported": ">=0.3.0",
            "patch_paths": ("src/**",),
        }
        arguments.update(overrides)
        return scope_rules.decide(**arguments)  # type: ignore[arg-type]

    def test_an_in_scope_report_names_its_fix_class(self) -> None:
        decision = self.make()

        assert decision.in_scope is True
        assert decision.fix_class == "bad_type_handling"
        assert bool(decision) is True

    def test_an_unstructured_report_cannot_be_localized(self) -> None:
        decision = self.make(fingerprint=fp.from_text("it broke"))

        assert decision.reason is Reason.INSUFFICIENT_REPORT

    def test_an_unstated_version_is_never_assumed_supported(self) -> None:
        assert self.make(reported_version=None).reason is Reason.UNSUPPORTED_VERSION

    def test_an_old_version_is_out_of_scope(self) -> None:
        decision = self.make(reported_version="0.1.0")

        assert decision.reason is Reason.UNSUPPORTED_VERSION
        assert "0.3.0" in decision.detail

    def test_an_exception_outside_the_fix_class_is_refused(self) -> None:
        decision = self.make(fingerprint=fp.from_text(TRACEBACK.replace("TypeError", "OSError")))

        assert decision.reason is Reason.OUT_OF_FIX_CLASS

    def test_a_frame_outside_the_patchable_tree_is_refused(self) -> None:
        decision = self.make(patch_paths=("src/other/**",))

        assert decision.reason is Reason.OUT_OF_ALLOWED_PATHS

    @pytest.mark.parametrize(
        ("exception", "fix_class"),
        [
            ("ValueError", "input_validation"),
            ("KeyError", "input_validation"),
            ("TypeError", "bad_type_handling"),
            ("AttributeError", "bad_type_handling"),
            ("OSError", None),
            (None, None),
        ],
    )
    def test_fix_class_mapping(self, exception: str | None, fix_class: str | None) -> None:
        assert scope_rules.classify_fix_class(exception) == fix_class
