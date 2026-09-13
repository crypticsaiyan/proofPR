"""Email validation."""

from __future__ import annotations


def validate_email(address: str) -> str:
    """Validate an email address and return it normalised.

    Args:
        address: The address to validate.

    Returns:
        The address, lowercased.

    Raises:
        ValueError: The address is not valid.
    """
    if "@" not in address:
        raise ValueError(f"invalid address: {address!r}")
    local, _, domain = address.partition("@")
    if not local or not domain or "." not in domain:
        raise ValueError(f"invalid address: {address!r}")
    return address.lower()
