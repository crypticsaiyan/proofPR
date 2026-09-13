"""The mutator alters only what the patch touched, and only in fixed ways."""

from __future__ import annotations

import ast

import pytest

from proofpr.proof import mutator

BEFORE = """\
def parse(month, day):
    length = LENGTHS.get(month)
    if day > length:
        raise ValueError("day out of range")
    return month, day
"""

AFTER = """\
def parse(month, day):
    length = LENGTHS.get(month)
    if length is None:
        raise ValueError(f"month {month} is out of range")
    if day > length:
        raise ValueError("day out of range")
    return month, day
"""


class TestChangedLines:
    """Only the patch's own lines are eligible."""

    def test_inserted_lines_are_reported(self) -> None:
        assert sorted(mutator.changed_lines(BEFORE, AFTER)) == [3, 4]

    def test_an_identical_file_changes_nothing(self) -> None:
        assert mutator.changed_lines(AFTER, AFTER) == frozenset()

    def test_a_new_file_is_entirely_changed(self) -> None:
        assert sorted(mutator.changed_lines("", "a = 1\nb = 2\n")) == [1, 2]


class TestGenerate:
    """Each mutant is a complete, parseable file differing in one place."""

    def test_mutants_are_scoped_to_the_changed_lines(self) -> None:
        mutants = mutator.generate(AFTER, mutator.changed_lines(BEFORE, AFTER))

        assert mutants
        assert all(mutant.line in {3, 4} for mutant in mutants)
        # Line 5's `day > length` predates the patch and is not this patch's to defend.
        assert not any("Gt becomes" in mutant.description for mutant in mutants)

    def test_the_guard_clause_and_its_raise_are_both_mutated(self) -> None:
        operators = {mutant.operator for mutant in mutator.generate(AFTER, frozenset({3, 4}))}

        assert "comparison" in operators
        assert "raise_removal" in operators

    def test_every_mutant_parses(self) -> None:
        for mutant in mutator.generate(AFTER, frozenset({3, 4})):
            ast.parse(mutant.code)

    def test_every_mutant_differs_from_the_original(self) -> None:
        normalised = ast.unparse(ast.parse(AFTER))

        for mutant in mutator.generate(AFTER, frozenset({3, 4})):
            assert mutant.code != normalised

    def test_generation_is_deterministic(self) -> None:
        first = mutator.generate(AFTER, frozenset({3, 4}))
        second = mutator.generate(AFTER, frozenset({3, 4}))

        assert [m.code for m in first] == [m.code for m in second]
        assert [m.operator for m in first] == [m.operator for m in second]

    def test_no_changed_lines_means_no_mutants(self) -> None:
        assert mutator.generate(AFTER, frozenset()) == []

    def test_a_file_that_does_not_parse_yields_nothing(self) -> None:
        assert mutator.generate("def broken(:", frozenset({1})) == []

    def test_the_number_of_mutants_is_capped(self) -> None:
        source = "\n".join(f"x{i} = {i} > {i + 1}" for i in range(40))

        mutants = mutator.generate(source, frozenset(range(1, 41)), limit=5)

        assert len(mutants) == 5

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("if a < b:\n    pass", "a <= b"),
            ("if a == b:\n    pass", "a != b"),
            ("if a is None:\n    pass", "a is not None"),
            ("x = a and b", "a or b"),
            ("x = True", "x = False"),
            ("x = 5", "x = 6"),
        ],
    )
    def test_each_operator_produces_the_expected_change(self, source: str, expected: str) -> None:
        mutants = mutator.generate(source, frozenset({1}))

        assert any(expected in mutant.code for mutant in mutants), [m.code for m in mutants]

    def test_a_returned_value_can_be_blanked(self) -> None:
        mutants = mutator.generate("def f():\n    return 1 + 1\n", frozenset({2}))

        assert any("return None" in mutant.code for mutant in mutants)

    def test_a_mutant_labels_where_and_what(self) -> None:
        mutant = mutator.generate("if a < b:\n    pass", frozenset({1}))[0]

        assert mutant.label.startswith("line 1: ")
        assert "Lt becomes LtE" in mutant.label
