"""Text normalisation."""

from __future__ import annotations


def slugify(text: str) -> str:
    """Turn a title into a slug.

    Args:
        text: The title.

    Returns:
        A lowercase, hyphenated slug.

    Raises:
        ValueError: The text contains nothing usable.
    """
    cleaned = "".join(character if character.isalnum() else "-" for character in text.lower())
    parts = [part for part in cleaned.split("-") if part]
    # Deliberate bug: an all-punctuation title yields an empty slug rather than
    # a clear error.
    return "-".join(parts)


def truncate(text: str, limit: int) -> str:
    """Truncate text to a limit, adding an ellipsis.

    Args:
        text: The text.
        limit: Maximum length including the ellipsis.

    Returns:
        The truncated text.

    Raises:
        ValueError: The limit is too small to truncate into.
    """
    if limit < 4:
        raise ValueError("limit must be at least 4")
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
