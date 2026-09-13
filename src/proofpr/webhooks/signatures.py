"""Webhook signature verification.

A webhook is an unauthenticated HTTP request that claims to come from GitHub or
Linear. Acting on one without checking the signature means anyone who learns the
URL can tell this agent that a pull request merged, which would make it notify
reporters and close issues on command.

Three rules hold for both providers:

1. The **raw body** is verified, before parsing. Re-serialising JSON changes
   bytes and would make every signature fail, or worse, make a mismatched body
   verify.
2. Comparison is constant time. A timing oracle on a signature check is a slow
   but real forgery.
3. A missing secret refuses everything rather than skipping the check. An
   unconfigured verifier that accepts requests is the most dangerous shape this
   code could take.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from proofpr.observability.logging import get_logger

logger = get_logger("proofpr.webhooks")

#: GitHub sends `sha256=<hex>`; the prefix is part of the header, not the digest.
GITHUB_PREFIX = "sha256="


@dataclass(frozen=True, slots=True)
class Verification:
    """Whether a request is genuine, and why not when it is not."""

    valid: bool
    reason: str = ""


def _compare(expected: str, provided: str) -> bool:
    """Compare two hex digests in constant time."""
    return hmac.compare_digest(expected, provided)


def _digest(secret: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 of a body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_github(secret: str, body: bytes, header: str | None) -> Verification:
    """Verify an `X-Hub-Signature-256` header.

    Args:
        secret: The webhook secret configured on the repository.
        body: The raw request body, exactly as received.
        header: The signature header, including its `sha256=` prefix.
    """
    if not secret:
        return Verification(False, "no GitHub webhook secret is configured")
    if not header:
        return Verification(False, "the request carried no signature")
    if not header.startswith(GITHUB_PREFIX):
        return Verification(False, "the signature is not sha256")

    if not _compare(_digest(secret, body), header.removeprefix(GITHUB_PREFIX)):
        return Verification(False, "the signature does not match the body")
    return Verification(True)


def verify_linear(secret: str, body: bytes, header: str | None) -> Verification:
    """Verify a `Linear-Signature` header, which is a bare hex digest."""
    if not secret:
        return Verification(False, "no Linear webhook secret is configured")
    if not header:
        return Verification(False, "the request carried no signature")
    if not _compare(_digest(secret, body), header.strip()):
        return Verification(False, "the signature does not match the body")
    return Verification(True)


def sign(secret: str, body: bytes, *, provider: str = "github") -> str:
    """Produce a signature, for tests and for local delivery replay.

    Exported deliberately: a verifier that cannot be exercised against a real
    signature tends to be a verifier nobody has actually tested.
    """
    digest = _digest(secret, body)
    return f"{GITHUB_PREFIX}{digest}" if provider == "github" else digest
