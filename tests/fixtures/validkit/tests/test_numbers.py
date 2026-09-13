"""The target repository's own suite for numbers."""

import pytest

from validkit import clamp, parse_port


def test_parses_a_port():
    assert parse_port("8080") == 8080


def test_rejects_a_port_below_range():
    with pytest.raises(ValueError):
        parse_port("0")


def test_clamps_into_range():
    assert clamp(11, 1, 10) == 10
