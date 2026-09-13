import pytest

from validkit import slugify


def test_a_title_with_no_usable_characters_raises_value_error():
    with pytest.raises(ValueError):
        slugify("!!!")
