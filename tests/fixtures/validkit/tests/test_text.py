"""The target repository's own suite for text."""

import pytest

from validkit import slugify, truncate


def test_slugifies_a_title():
    assert slugify("Hello, World!") == "hello-world"


def test_truncates_long_text():
    assert truncate("abcdefgh", 5) == "abcd\u2026"


def test_rejects_a_tiny_limit():
    with pytest.raises(ValueError):
        truncate("abc", 2)
