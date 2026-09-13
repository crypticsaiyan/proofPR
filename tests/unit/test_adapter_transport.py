"""Transport behaviour: retries, error mapping, fault injection, guarded writes."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from proofpr.adapters.base import FaultInjector
from proofpr.adapters.github import API_ROOT, GitHubAdapter
from proofpr.domain.errors import GuardBlockedError, PermanentAppError, RetryableAppError
from proofpr.domain.models import WriteIntent
from proofpr.guard import Guard


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    """Intercept every call to the GitHub API."""
    with respx.mock(base_url=API_ROOT, assert_all_called=False) as router:
        yield router


def build(guard: Guard, *, faults: FaultInjector | None = None) -> GitHubAdapter:
    """Build a GitHub adapter with a backoff short enough for a test suite."""
    return GitHubAdapter(
        token="ghp_notarealtokenvalue",
        owner="acme",
        repo="validkit",
        guard=guard,
        faults=faults,
        attempts=3,
        backoff_initial=0.001,
    )


async def test_a_rate_limit_is_retried_and_then_succeeds(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("/repos/acme/validkit").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"default_branch": "main"}),
        ]
    )

    response = await build(guard).request("GET", "/repos/acme/validkit", operation="github.get")

    assert response.json()["default_branch"] == "main"
    assert route.call_count == 2


async def test_retries_stop_and_the_error_surfaces(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("/repos/acme/validkit").mock(return_value=httpx.Response(503))

    with pytest.raises(RetryableAppError) as excinfo:
        await build(guard).request("GET", "/repos/acme/validkit", operation="github.get")

    assert route.call_count == 3
    assert excinfo.value.status == 503
    assert excinfo.value.app == "github"


async def test_client_errors_are_permanent_and_never_retried(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    route = mock_api.get("/repos/acme/validkit").mock(
        return_value=httpx.Response(401, json={"message": "bad credentials"})
    )

    with pytest.raises(PermanentAppError) as excinfo:
        await build(guard).request("GET", "/repos/acme/validkit", operation="github.get")

    assert route.call_count == 1
    assert excinfo.value.status == 401


async def test_a_timeout_is_retryable(guard: Guard, mock_api: respx.MockRouter) -> None:
    mock_api.get("/repos/acme/validkit").mock(side_effect=httpx.ReadTimeout("too slow"))

    with pytest.raises(RetryableAppError, match="timeout"):
        await build(guard).request("GET", "/repos/acme/validkit", operation="github.get")


async def test_writes_are_not_retried_even_when_the_status_is_retryable(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    # A write that may not be repeated is sent once, and the caller decides what
    # to do. Repeating it blindly is how duplicate issues and branches happen.
    route = mock_api.post("/repos/acme/validkit/issues/1/comments").mock(
        return_value=httpx.Response(500)
    )
    intent = WriteIntent(
        run_id="r-test",
        app="github",
        operation="github.comment",
        target="acme/validkit#1",
        payload={"number": 1, "body": "hello"},
    )

    with pytest.raises(RetryableAppError):
        await build(guard).comment(intent)

    assert route.call_count == 1


async def test_injected_faults_exercise_the_real_retry_path(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    mock_api.get("/repos/acme/validkit").mock(return_value=httpx.Response(200, json={}))
    faults = FaultInjector(plan={"github.get": [429, "timeout"]})

    await build(guard, faults=faults).request("GET", "/repos/acme/validkit", operation="github.get")

    assert [key for key, _ in faults.fired] == ["github.get", "github.get"]


async def test_a_refused_write_never_reaches_the_network(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("/repos/acme/validkit/git/refs").mock(
        return_value=httpx.Response(201, json={"object": {"sha": "abc"}})
    )
    intent = WriteIntent(
        run_id="r-test",
        app="github",
        operation="github.create_branch",
        target="acme/validkit@main",
        payload={"branch": "main"},
    )

    with pytest.raises(GuardBlockedError):
        await build(guard).create_branch(intent)

    assert route.call_count == 0


async def test_a_payload_carrying_a_secret_never_reaches_the_network(
    guard: Guard, mock_api: respx.MockRouter
) -> None:
    route = mock_api.post("/repos/acme/validkit/issues/1/comments").mock(
        return_value=httpx.Response(201, json={"id": 1, "html_url": "x"})
    )
    intent = WriteIntent(
        run_id="r-test",
        app="github",
        operation="github.comment",
        target="acme/validkit#1",
        payload={"number": 1, "body": "token is ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
    )

    with pytest.raises(GuardBlockedError):
        await build(guard).comment(intent)

    assert route.call_count == 0


async def test_an_intent_for_another_application_is_rejected(guard: Guard) -> None:
    intent = WriteIntent(
        run_id="r-test",
        app="linear",
        operation="linear.create_issue",
        target="TEAM",
        payload={},
    )

    with pytest.raises(PermanentAppError, match="routed to the github adapter"):
        await build(guard).guarded_write(intent, _unreachable)


async def _unreachable() -> tuple[str, str | None]:
    """Fail loudly if a refused write is ever performed."""
    raise AssertionError("the write should never have been performed")
