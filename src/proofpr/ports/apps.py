"""Protocols for the three external applications, plus the model and the sandbox.

Steps depend on these protocols and never on a concrete adapter. Two things
implement each one: a real adapter in :mod:`proofpr.adapters`, and a fake in
``tests/fixtures``. The contract tests assert both behave the same way, which is
what lets the entire pipeline run without credentials.

Only writes appear in :data:`WRITE_OPERATIONS` on each adapter and in
``config/policy.yaml``. Reads are unrestricted; they cannot change the world.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from proofpr.domain.models import HealthCheck, UntrustedText, WriteIntent, WriteResult


@runtime_checkable
class AppPort(Protocol):
    """Common surface every application adapter provides."""

    app: str

    async def health(self) -> list[HealthCheck]:
        """Prove the credential works by doing one write and reading it back.

        Returns:
            One entry per check performed. An adapter that cannot reach its
            application returns a failing check rather than raising, so
            ``doctor`` can report on every application in one pass.
        """
        ...

    async def readback(self, result: WriteResult) -> WriteResult:
        """Re-read a write and return it confirmed, or raise if it disagrees.

        Raises:
            VerificationError: The observed state differs from the intent.
        """
        ...

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Return this write if the application already holds it, else None.

        Called before retrying a write whose outcome is unknown: a timeout, a
        5xx, or a crash between sending and recording. Operations that are
        idempotent by nature (an edit, a status change, a reaction) return None,
        because repeating them is already safe.

        Raises:
            RetryableAppError: The application could not be asked. The caller
                must not re-send the write, since its outcome is still unknown.
        """
        ...


@runtime_checkable
class DiscordPort(AppPort, Protocol):
    """Intake, live status, reporter conversation."""

    async def fetch_message(self, channel_id: int, message_id: int) -> UntrustedText:
        """Fetch a message body as untrusted text."""
        ...

    async def reply(self, intent: WriteIntent) -> WriteResult:
        """Reply in the thread of this run's intake message."""
        ...

    async def edit_status_message(self, intent: WriteIntent) -> WriteResult:
        """Edit this run's own status message in place."""
        ...

    async def add_reaction(self, intent: WriteIntent) -> WriteResult:
        """Acknowledge the intake message with a reaction."""
        ...


@runtime_checkable
class GitHubPort(AppPort, Protocol):
    """Branches, pull requests, and the check runs that judge them."""

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Search issues and pull requests for the exists check."""
        ...

    async def list_commits_between(self, base: str, head: str) -> list[dict[str, Any]]:
        """List commits between two refs, for `already_fixed` detection."""
        ...

    async def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        """Return check runs for a ref. CI status is read from here, never inferred."""
        ...

    async def create_branch(self, intent: WriteIntent) -> WriteResult:
        """Create this run's branch from the default branch."""
        ...

    async def push(self, intent: WriteIntent) -> WriteResult:
        """Commit file contents to this run's branch."""
        ...

    async def open_pr(self, intent: WriteIntent) -> WriteResult:
        """Open a draft pull request carrying the proof block."""
        ...

    async def mark_ready(self, intent: WriteIntent) -> WriteResult:
        """Take this run's pull request out of draft, only after CI succeeds."""
        ...

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on this run's pull request."""
        ...


@runtime_checkable
class LinearPort(AppPort, Protocol):
    """The issue of record for every run, including the ones that write no code."""

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Search the configured team for duplicate candidates."""
        ...

    async def create_issue(self, intent: WriteIntent) -> WriteResult:
        """File an issue in the configured team."""
        ...

    async def update_issue(self, intent: WriteIntent) -> WriteResult:
        """Update the issue created or matched by this run."""
        ...

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on the issue created or matched by this run."""
        ...

    async def attach_url(self, intent: WriteIntent) -> WriteResult:
        """Attach a pull request or Discord link to this run's issue."""
        ...


@runtime_checkable
class ModelPort(Protocol):
    """A model call with a fixed output schema.

    The model returns data. It never names an operation, a repository, a branch,
    or an issue that the state machine had not already chosen.
    """

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[Any],
        tier: str = "cheap",
        max_tokens: int = 2048,
    ) -> tuple[Any, dict[str, Any]]:
        """Return a validated object and the call's usage record.

        Args:
            system: System prompt. Contains no application text.
            user: User prompt. Untrusted text appears only inside delimited
                blocks produced by :mod:`proofpr.guard.sanitize`.
            schema: Pydantic model the response must validate against.
            tier: ``cheap`` or ``strong``.
            max_tokens: Output cap.

        Returns:
            The validated object, and a usage dict with token counts, cost, the
            model name, and the prompt version hash.
        """
        ...


@runtime_checkable
class SandboxPort(Protocol):
    """Execution of model-written code, always inside a container."""

    async def run(
        self,
        *,
        worktree: str,
        command: list[str],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Run a command against a worktree in an isolated container.

        Returns:
            A dict with ``exit_code``, ``stdout``, ``stderr``, ``duration_ms``,
            and ``timed_out``. Output is truncated and must be sanitized before
            it reaches any prompt.
        """
        ...
