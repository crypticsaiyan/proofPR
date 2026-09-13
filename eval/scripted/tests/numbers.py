import pytest

from validkit import parse_port


def test_the_port_above_the_maximum_is_rejected():
    with pytest.raises(ValueError):
        parse_port("65536")
