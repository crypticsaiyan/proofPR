import pytest

from validkit import parse_date


def test_an_invalid_month_raises_value_error():
    with pytest.raises(ValueError):
        parse_date("2024-13-01")
