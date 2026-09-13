"""Error hierarchy.

Every error raised by ProofPR carries a stable `code`. Codes appear in the
ledger, in structured logs, and in evaluation output, so they are treated as a
wire format and never renamed casually.

Adapters map transport failures onto :class:`RetryableAppError` or
:class:`PermanentAppError`. Only retryable errors are retried, and only for
operations that are idempotent or guarded by a run marker.
"""

from __future__ import annotations


class ProofPRError(Exception):
    """Base class for every error raised by this package."""

    code: str = "proofpr_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description, safe to show a maintainer.
            code: Overrides the class-level stable code when set.
        """
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        """Return the message prefixed by the stable code."""
        return f"[{self.code}] {super().__str__()}"


class ConfigurationError(ProofPRError):
    """Settings are missing, malformed, or mutually inconsistent."""

    code = "configuration_error"


class AppError(ProofPRError):
    """An external application rejected or failed a call."""

    code = "app_error"

    def __init__(
        self,
        message: str,
        *,
        app: str,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description.
            app: Adapter name, for example ``github``.
            status: HTTP status when the failure came from a response.
            code: Overrides the class-level stable code when set.
        """
        super().__init__(message, code=code)
        self.app = app
        self.status = status


class RetryableAppError(AppError):
    """Transient failure: rate limit, 5xx, timeout, or connection reset."""

    code = "retryable_app_error"


class PermanentAppError(AppError):
    """Failure that will not succeed on retry: auth, validation, or not found."""

    code = "permanent_app_error"


class GuardBlockedError(ProofPRError):
    """A write was refused by the operation allowlist or the egress scanner.

    Raised only by :mod:`proofpr.guard`. Always recorded as a ``guard_blocked``
    ledger event before it propagates, because the count of blocked writes is a
    reported evaluation metric.
    """

    code = "guard_blocked"

    def __init__(self, message: str, *, operation: str, rule: str) -> None:
        """Initialise the error.

        Args:
            message: Why the write was refused.
            operation: The operation that was attempted, for example
                ``github.open_pr``.
            rule: The allowlist or egress rule that refused it.
        """
        super().__init__(message)
        self.operation = operation
        self.rule = rule


class SandboxError(ProofPRError):
    """The container sandbox failed to start, timed out, or was killed."""

    code = "sandbox_error"


class VerificationError(ProofPRError):
    """Readback after a write did not match the intended state."""

    code = "verification_error"
