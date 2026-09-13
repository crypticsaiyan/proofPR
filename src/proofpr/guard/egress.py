"""Outbound payload scanning.

The last thing between a payload and the network. It looks for the planted
canary, for credential-shaped strings, and for the literal value of any
environment variable that looks like a secret.

Scanning values rather than trusting call sites is deliberate: the payload may
contain sandbox output, a model-written patch, or a stack frame from a stranger,
and none of those can be reasoned about at the call site.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

from proofpr.domain.errors import GuardBlockedError
from proofpr.domain.models import WriteIntent

#: Credential shapes, independent of the policy file so a policy edit cannot
#: weaken them by omission.
BUILTIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"lin_api_[A-Za-z0-9]{20,}"),
    re.compile(r"sntrys_[A-Za-z0-9_\-.]{20,}"),
    re.compile(r"sk-or-[A-Za-z0-9\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\."),  # JWT
    # Discord bot tokens: base64 user id, timestamp, hmac.
    re.compile(r"[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"),
)

#: Environment variables whose literal value must never appear in a payload.
SECRET_ENV_SUFFIXES = ("TOKEN", "KEY", "SECRET", "PASSWORD")

#: Values short enough that matching them would produce constant false positives.
MIN_SECRET_VALUE_LENGTH = 12

DEFAULT_MAX_PAYLOAD_BYTES = 65536


class EgressScanner:
    """Scans outbound payloads for secrets before they leave the process."""

    def __init__(
        self,
        *,
        canary: str | None = None,
        extra_literals: Iterable[str] = (),
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Build a scanner.

        Args:
            canary: The planted fake secret. Its appearance in any payload means
                exfiltration succeeded somewhere upstream.
            extra_literals: Additional literal strings from the policy file.
            max_payload_bytes: Payloads larger than this are refused; an
                oversized body is the cheapest way to smuggle a file out.
            environ: Environment to read secret values from. Defaults to the
                real one.
        """
        self.canary = canary
        self.max_payload_bytes = max_payload_bytes
        self._environ = dict(os.environ if environ is None else environ)
        self._literals = tuple(literal for literal in extra_literals if literal)

    @classmethod
    def from_policy(
        cls,
        egress_section: Mapping[str, Any],
        *,
        canary: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> EgressScanner:
        """Build a scanner from the ``egress`` section of ``config/policy.yaml``."""
        literals = [
            item for item in egress_section.get("reject_if_contains", []) if item != "canary_value"
        ]
        return cls(
            canary=canary,
            extra_literals=literals,
            max_payload_bytes=int(
                egress_section.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES)
            ),
            environ=environ,
        )

    def secret_environment_values(self) -> list[str]:
        """Return the literal secret values present in the environment."""
        return [
            value
            for key, value in self._environ.items()
            if key.endswith(SECRET_ENV_SUFFIXES) and len(value) >= MIN_SECRET_VALUE_LENGTH
        ]

    def scan(self, intent: WriteIntent) -> None:
        """Refuse the write if its payload carries anything secret.

        Args:
            intent: The write about to be attempted.

        Raises:
            GuardBlockedError: The payload is oversized or contains the canary,
                a credential-shaped string, or an environment secret value.
        """
        text = self._flatten(intent.payload)
        size = len(text.encode("utf-8"))
        if size > self.max_payload_bytes:
            raise GuardBlockedError(
                f"payload is {size} bytes, the limit is {self.max_payload_bytes}",
                operation=intent.operation,
                rule="max_payload_bytes",
            )

        if self.canary and self.canary in text:
            raise GuardBlockedError(
                "payload contains the canary value, which means a secret reached it",
                operation=intent.operation,
                rule="canary",
            )

        for literal in self._literals:
            if literal in text:
                raise GuardBlockedError(
                    f"payload contains a forbidden literal: {literal!r}",
                    operation=intent.operation,
                    rule="reject_if_contains",
                )

        for pattern in BUILTIN_PATTERNS:
            if pattern.search(text):
                raise GuardBlockedError(
                    "payload contains a credential-shaped string",
                    operation=intent.operation,
                    rule="credential_pattern",
                )

        for value in self.secret_environment_values():
            if value in text:
                raise GuardBlockedError(
                    "payload contains the value of a secret environment variable",
                    operation=intent.operation,
                    rule="environment_secret",
                )

    @staticmethod
    def _flatten(payload: object) -> str:
        """Render a payload as one string, including nested keys and values."""
        parts: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, Mapping):
                for key, value in node.items():
                    parts.append(str(key))
                    walk(value)
            elif isinstance(node, str):
                parts.append(node)
            elif isinstance(node, bytes):
                parts.append(node.decode("utf-8", errors="replace"))
            elif isinstance(node, Iterable):
                for item in node:
                    walk(item)
            else:
                parts.append(str(node))

        walk(payload)
        return "\n".join(parts)
