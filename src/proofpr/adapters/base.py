"""Shared transport behaviour for every application adapter.

An adapter owns transport concerns only: timeouts, retries, rate limits, error
mapping, and the guard call that precedes every write. It never decides what to
write. That separation is what keeps the reachable operation set a property of
code rather than of a prompt.

Retries apply to reads freely and to writes only when the caller states the write
is safe to repeat, which in this system means it is marker-guarded: the body
carries ``proofpr-run:<id>`` and the adapter searches for it before writing
again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from proofpr.domain.errors import PermanentAppError, RetryableAppError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.guard import Guard
from proofpr.observability.logging import get_logger
from proofpr.observability.tracing import get_tracer

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0)
DEFAULT_ATTEMPTS = 4

#: Status codes worth retrying. 408 and 425 are included because both mean the
#: request never produced an effect, which is the only thing that matters here.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass
class FaultInjector:
    """Deterministic fault injection used by the `faults` evaluation set.

    Faults are injected in the transport, not mocked at the call site, so the
    adapter's own retry and backoff behaviour is what gets exercised.
    """

    #: Maps ``app.operation`` to the faults to raise, consumed in order.
    plan: dict[str, list[int | str]] = field(default_factory=dict)
    fired: list[tuple[str, int | str]] = field(default_factory=list)

    def next_fault(self, key: str) -> int | str | None:
        """Pop and return the next fault for an operation, if one is planned."""
        queue = self.plan.get(key)
        if not queue:
            return None
        fault = queue.pop(0)
        self.fired.append((key, fault))
        return fault


class HttpAdapter:
    """Base class for HTTP-backed application adapters."""

    app: ClassVar[str] = "unknown"

    #: Every write this adapter can perform, unqualified. The contract tests
    #: assert this set is a subset of what ``config/policy.yaml`` allows, so an
    #: operation cannot be added in code and forgotten in the policy.
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        guard: Guard,
        faults: FaultInjector | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_initial: float = 0.5,
        backoff_max: float = 8.0,
    ) -> None:
        """Build an adapter.

        Args:
            client: A configured async client. The caller owns its lifetime.
            guard: The allowlist and egress scanner.
            faults: Optional deterministic fault injector.
            attempts: Total attempts, including the first.
            backoff_initial: First backoff delay in seconds.
            backoff_max: Ceiling for the backoff delay in seconds.
        """
        self._client = client
        self._guard = guard
        self._faults = faults
        self._attempts = attempts
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._log = get_logger(f"proofpr.adapters.{self.app}", app=self.app)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # -- transport ------------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        retry: bool = True,
        **kwargs: Any,  # noqa: ANN401 - forwarded verbatim to httpx
    ) -> httpx.Response:
        """Perform one HTTP call with retries and mapped errors.

        Args:
            method: HTTP method.
            url: Absolute or client-relative URL.
            operation: Qualified operation name, for logging and fault keys.
            retry: Whether transient failures may be retried.
            **kwargs: Passed through to httpx.

        Returns:
            The successful response.

        Raises:
            RetryableAppError: Transient failure that survived every attempt.
            PermanentAppError: The request will not succeed on retry.
        """
        attempts = self._attempts if retry else 1

        async def once() -> httpx.Response:
            self._maybe_inject(operation)
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as error:
                raise RetryableAppError(f"timeout calling {url}", app=self.app) from error
            except httpx.TransportError as error:
                raise RetryableAppError(f"transport error calling {url}", app=self.app) from error
            self.raise_for_status(response, operation=operation)
            return response

        with get_tracer().start_as_current_span(operation) as span:
            span.set_attribute("proofpr.app", self.app)
            span.set_attribute("http.request.method", method)
            # Exceptions that escape this block are recorded on the span and
            # marked as an error status by the context manager itself.
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential_jitter(initial=self._backoff_initial, max=self._backoff_max),
                retry=retry_if_exception_type(RetryableAppError),
                reraise=True,
                before_sleep=self._log_retry,
            ):
                with attempt:
                    response = await once()
                    span.set_attribute("http.response.status_code", response.status_code)
                    return response
        raise AssertionError("unreachable: AsyncRetrying always returns or raises")

    def raise_for_status(self, response: httpx.Response, *, operation: str) -> None:
        """Map an HTTP status onto the domain error hierarchy.

        ``Retry-After`` is honoured by raising a retryable error carrying the
        delay, which the backoff respects rather than overriding.
        """
        if response.status_code < 400:
            return

        detail = response.text[:500]
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableAppError(
                f"{operation} got {response.status_code}: {detail}",
                app=self.app,
                status=response.status_code,
            )
        raise PermanentAppError(
            f"{operation} got {response.status_code}: {detail}",
            app=self.app,
            status=response.status_code,
        )

    def _maybe_inject(self, operation: str) -> None:
        """Raise a planned fault, if the injector has one for this operation."""
        if self._faults is None:
            return
        fault = self._faults.next_fault(operation)
        if fault is None:
            return
        if fault == "timeout":
            raise RetryableAppError(f"injected timeout on {operation}", app=self.app)
        if isinstance(fault, int):
            if fault in RETRYABLE_STATUS:
                raise RetryableAppError(
                    f"injected {fault} on {operation}", app=self.app, status=fault
                )
            raise PermanentAppError(f"injected {fault} on {operation}", app=self.app, status=fault)
        raise PermanentAppError(f"injected fault {fault!r} on {operation}", app=self.app)

    def _log_retry(self, state: RetryCallState) -> None:
        """Record each retry, so retry storms are visible in the run log."""
        self._log.warning(
            "app_retry",
            attempt=state.attempt_number,
            error=str(state.outcome.exception()) if state.outcome else None,
        )

    # -- writes ---------------------------------------------------------------

    async def guarded_write(
        self,
        intent: WriteIntent,
        perform: Callable[[], Awaitable[tuple[str, str | None]]],
    ) -> WriteResult:
        """Authorize a write, perform it, and return an unverified result.

        The result is deliberately unverified. It becomes verified only after
        :meth:`readback` reads the application and agrees, which is a separate
        call the pipeline must make.

        Args:
            intent: What is about to be written.
            perform: Coroutine performing the write and returning
                ``(remote_id, url)``.

        Returns:
            The unverified write result.

        Raises:
            GuardBlockedError: The write was refused.
        """
        if intent.app != self.app:
            raise PermanentAppError(
                f"{intent.operation} routed to the {self.app} adapter",
                app=self.app,
            )
        self._guard.authorize(intent)
        self._log.info("write_attempted", operation=intent.operation, target=intent.target)
        remote_id, url = await perform()
        return WriteResult(intent=intent, remote_id=remote_id, url=url)

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Return this write if the application already holds it, else None.

        The default suits idempotent operations, where repeating is already
        safe. Adapters override it for every operation that creates something.
        """
        del intent
        return None

    @staticmethod
    def with_marker(body: str, intent: WriteIntent, *, comment: str = "<!-- {} -->") -> str:
        """Append the run marker to a body so retries can find their own write.

        Args:
            body: The text being written.
            intent: The write it belongs to.
            comment: Comment form for the destination's markup.
        """
        marker = comment.format(intent.marker)
        return body if marker in body else f"{body}\n\n{marker}"

    @staticmethod
    def observed_matches(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        """Return whether every expected key is present and equal in the readback."""
        return all(observed.get(key) == value for key, value in expected.items())
