"""The target repository's own suite. It passes, which the gate requires."""

import pytest

from validkit import parse_date


def test_parses_a_valid_date():
    assert parse_date("2024-02-11") == (2024, 2, 11)


def test_rejects_a_day_out_of_range():
    with pytest.raises(ValueError):
        parse_date("2024-02-30")


def test_rejects_a_malformed_date():
    with pytest.raises(ValueError):
        parse_date("not-a-date-at-all-really")
