"""Value objects shared by the pipeline, the guard, and the adapters.

Everything here is frozen. A run's mutable state lives in the ledger, never in
an object that a step could quietly edit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

AppName = Literal["discord", "github", "linear"]


class Frozen(BaseModel):
    """Base for immutable models with strict validation."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class WriteIntent(Frozen):
    """A single intended write to an external application.

    Every write in ProofPR is expressed as one of these before it happens, which
    is what makes the allowlist and the egress scan possible at all. Adapters
    cannot write without one.
    """

    run_id: str = Field(min_length=1)
    app: AppName
    operation: str = Field(min_length=1)
    target: str = Field(min_length=1, description="What is written to, for example owner/repo#42.")
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("operation")
    @classmethod
    def _qualified(cls, value: str) -> str:
        """Require ``app.operation`` form so logs and policy keys always agree."""
        if "." not in value:
            raise ValueError(f"operation must be qualified as app.operation, got {value!r}")
        return value

    @property
    def short_operation(self) -> str:
        """Return the operation without its application prefix."""
        return self.operation.split(".", 1)[1]

    @property
    def marker(self) -> str:
        """Return the idempotency marker embedded in every written body."""
        return f"proofpr-run:{self.run_id}"


class WriteResult(Frozen):
    """The outcome of a write, after readback.

    A write is not finished when the API returns 200. It is finished when the
    application is read back and agrees.
    """

    intent: WriteIntent
    remote_id: str
    url: str | None = None
    verified: bool = False
    observed: dict[str, Any] = Field(default_factory=dict)

    def confirm(self, observed: dict[str, Any]) -> Self:
        """Return a copy marked verified with the state read back from the app."""
        return self.model_copy(update={"verified": True, "observed": observed})


class HealthCheck(Frozen):
    """One `proofpr doctor` result."""

    app: str
    check: str
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0


class UntrustedText(Frozen):
    """Text that came from an external application.

    The type exists so that passing raw application text into a prompt is a type
    error rather than an oversight. Rendering it for a model always goes through
    :mod:`proofpr.guard.sanitize`.
    """

    source: str = Field(description="Where it came from, for example discord.message.content.")
    value: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        """Return a redacted form, so accidental interpolation is visible."""
        return f"<untrusted {self.source} {len(self.value)} chars>"
