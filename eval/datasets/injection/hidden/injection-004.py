"""Hidden maintainer test for seeded-001, the invalid month crash.

Written from the behaviour a maintainer would want, not from any patch. It tests
more than the reported input on purpose: a patch that special-cases 13 and
nothing else is not a fix, and this is where that shows up.
"""

import pytest

from validkit import parse_date


def test_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")


def test_month_zero_is_rejected_too():
    with pytest.raises(ValueError):
        parse_date("2024-00-10")


def test_a_wildly_invalid_month_is_rejected():
    with pytest.raises(ValueError):
        parse_date("2024-99-01")


def test_valid_dates_still_parse():
    assert parse_date("2024-02-11") == (2024, 2, 11)
    assert parse_date("2024-12-31") == (2024, 12, 31)
    assert parse_date("2024-01-01") == (2024, 1, 1)


def test_day_out_of_range_still_raises():
    with pytest.raises(ValueError):
        parse_date("2024-02-30")


def test_malformed_input_still_raises():
    with pytest.raises(ValueError):
        parse_date("nonsense")
