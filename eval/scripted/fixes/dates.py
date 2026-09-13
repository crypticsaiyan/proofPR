"""Date parsing."""

from __future__ import annotations

MONTH_LENGTHS = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _month_length(month: int) -> int | None:
    """Return the number of days in a month, or None when the month is invalid."""
    return MONTH_LENGTHS.get(month)


def parse_date(text: str) -> tuple[int, int, int]:
    """Parse an ISO-like date into its parts.

    Args:
        text: A date in ``YYYY-MM-DD`` form.

    Returns:
        The year, month, and day.

    Raises:
        ValueError: The text is not a date in the expected shape.
    """
    parts = text.split("-")
    if len(parts) != 3:
        raise ValueError(f"expected YYYY-MM-DD, got {text!r}")
    year, month, day = (int(part) for part in parts)

    length = _month_length(month)
    if length is None:
        raise ValueError(f"month {month} is out of range")
    if day > length:
        raise ValueError(f"day {day} is out of range for month {month}")
    return year, month, day
