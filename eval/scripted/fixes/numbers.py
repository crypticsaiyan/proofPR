"""Numeric parsing and range checks."""

from __future__ import annotations


def parse_port(text: str) -> int:
    """Parse a TCP port.

    Args:
        text: The port as written by a user.

    Returns:
        The port number.

    Raises:
        ValueError: The text is not a port in range.
    """
    value = int(text)
    if value < 1 or value > 65535:
        raise ValueError(f"port {value} is out of range")
    return value


def clamp(value: int, low: int, high: int) -> int:
    """Clamp a value into a range.

    Args:
        value: The value to clamp.
        low: Lower bound.
        high: Upper bound.

    Returns:
        The clamped value.

    Raises:
        ValueError: The bounds are the wrong way round.
    """
    if low > high:
        raise ValueError("low must not exceed high")
    return max(low, min(value, high))
