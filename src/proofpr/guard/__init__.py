"""The security boundary: sanitizer, operation allowlist, and egress scanner.

Every write passes :meth:`Guard.authorize`. Every outbound payload passes the
egress scan inside it. Every piece of application-sourced text passes
:func:`~proofpr.guard.sanitize.sanitize` before reaching a prompt.

Refusals raise :class:`~proofpr.domain.errors.GuardBlockedError` and are recorded
as ``guard_blocked`` events, because the count of blocked writes is a reported
evaluation metric rather than a log curiosity.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from proofpr.domain.errors import GuardBlockedError
from proofpr.domain.models import WriteIntent
from proofpr.guard.egress import EgressScanner
from proofpr.guard.policy import Policy
from proofpr.guard.sanitize import Sanitized, sanitize

__all__ = ["EgressScanner", "Guard", "Policy", "Sanitized", "sanitize"]


class Guard:
    """Allowlist and egress scan, applied in that order."""

    def __init__(
        self,
        policy: Policy,
        scanner: EgressScanner,
        *,
        branch_prefix: str = "proofpr/",
        on_block: Callable[[WriteIntent, str, str], None] | None = None,
    ) -> None:
        """Build a guard.

        Args:
            policy: The loaded operation allowlist.
            scanner: The outbound payload scanner.
            branch_prefix: Prefix every branch this agent creates must carry.
            on_block: Called with the intent, the rule, and the message whenever
                a write is refused. The pipeline uses it to write the ledger
                event before the error propagates.
        """
        self.policy = policy
        self.scanner = scanner
        self.branch_prefix = branch_prefix
        self._on_block = on_block

    @classmethod
    def load(
        cls,
        *,
        policy_path: Path | None = None,
        canary: str | None = None,
        branch_prefix: str = "proofpr/",
        on_block: Callable[[WriteIntent, str, str], None] | None = None,
    ) -> Guard:
        """Load a guard from ``config/policy.yaml``."""
        policy = Policy.load(policy_path)
        scanner = EgressScanner.from_policy(policy.egress, canary=canary)
        return cls(policy, scanner, branch_prefix=branch_prefix, on_block=on_block)

    def authorize(self, intent: WriteIntent) -> None:
        """Permit the write, or refuse it.

        The allowlist runs first: an operation that is not permitted at all
        should be refused before its payload is even considered.

        Raises:
            GuardBlockedError: The write is not permitted.
        """
        try:
            self.policy.check(intent, branch_prefix=self.branch_prefix)
            self.scanner.scan(intent)
        except GuardBlockedError as blocked:
            if self._on_block is not None:
                self._on_block(intent, blocked.rule, str(blocked))
            raise
