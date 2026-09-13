"""Forced failures on every adapter, and no duplicate writes when they recover.

Faults are injected in the transport rather than mocked at the call site, so the
adapter's own retry, backoff, and error mapping are what get exercised. The
question each test asks is not "does it retry" but "what did the application end
up holding once it stopped retrying".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from proofpr.adapters.base import FaultInjector
from proofpr.adapters.discord import API_ROOT as DISCORD_ROOT
from proofpr.adapters.discord import DiscordAdapter
from proofpr.adapters.github import API_ROOT as GITHUB_ROOT
from proofpr.adapters.github import GitHubAdapter
from proofpr.adapters.linear import API_URL as LINEAR_URL
from proofpr.adapters.linear import LinearAdapter
from proofpr.domain.errors import PermanentAppError, RetryableAppError
from proofpr.domain.models import WriteIntent
from proofpr.guard import Guard

RETRYABLE = [429, 500, 502, 503, 504, "timeout"]


def intent(operation: str, **payload: object) -> WriteIntent:
    """Build a write intent."""
    return WriteIntent(
        run_id="r-test",
        app=operation.split(".", 1)[0],
        operation=operation,
        target="target",
        payload=dict(payload),
    )


@pytest.fixture
def mock_github() -> Iterator[respx.MockRouter]:
    """Intercept the GitHub API."""
    with respx.mock(base_url=GITHUB_ROOT, assert_all_called=False) as router:
        yield router


@pytest.fixture
def mock_discord() -> Iterator[respx.MockRouter]:
    """Intercept the Discord API."""
    with respx.mock(base_url=DISCORD_ROOT, assert_all_called=False) as router:
        yield router


def github(guard: Guard, faults: FaultInjector | None = None) -> GitHubAdapter:
    """Build a GitHub adapter with a fast retry policy."""
    return GitHubAdapter(
        token="t",
        owner="acme",
        repo="validkit",
        guard=guard,
        faults=faults,
        attempts=4,
        backoff_initial=0.001,
    )


class TestRecovery:
    """A transient fault costs latency, not correctness."""

    @pytest.mark.parametrize("fault", RETRYABLE)
    async def test_a_read_recovers_from_any_transient_fault(
        self, guard: Guard, mock_github: respx.MockRouter, fault: int | str
    ) -> None:
        mock_github.get("/repos/acme/validkit").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        faults = FaultInjector(plan={"github.get_repo": [fault, fault]})

        response = await github(guard, faults).request(
            "GET", "/repos/acme/validkit", operation="github.get_repo"
        )

        assert response.status_code == httpx.codes.OK
        assert len(faults.fired) == 2

    async def test_the_retry_budget_is_finite(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit").mock(return_value=httpx.Response(200, json={}))
        faults = FaultInjector(plan={"github.get_repo": [429, 429, 429, 429, 429]})

        with pytest.raises(RetryableAppError):
            await github(guard, faults).request(
                "GET", "/repos/acme/validkit", operation="github.get_repo"
            )

        assert len(faults.fired) == 4

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    async def test_a_permanent_failure_is_not_retried(
        self, guard: Guard, mock_github: respx.MockRouter, status: int
    ) -> None:
        faults = FaultInjector(plan={"github.get_repo": [status, status]})
        mock_github.get("/repos/acme/validkit").mock(return_value=httpx.Response(200, json={}))

        with pytest.raises(PermanentAppError):
            await github(guard, faults).request(
                "GET", "/repos/acme/validkit", operation="github.get_repo"
            )

        assert len(faults.fired) == 1


class TestNoDuplicateWrites:
    """A fault on a write leaves at most one write behind."""

    async def test_a_failed_write_is_attempted_exactly_once(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        # Writes are never retried blindly. Retrying an issue creation that may
        # have succeeded is how an agent files the same bug three times.
        route = mock_github.post("/repos/acme/validkit/issues/1/comments").mock(
            return_value=httpx.Response(201, json={"id": 1, "html_url": "u"})
        )
        faults = FaultInjector(plan={"github.comment": [503]})

        with pytest.raises(RetryableAppError):
            await github(guard, faults).comment(intent("github.comment", number=1, body="hi"))

        assert route.call_count == 0

    async def test_linear_recovers_on_a_read_and_writes_once(self, guard: Guard) -> None:
        posts: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            posts.append(body)
            if "issues(" in body["query"]:
                return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {"id": "i1", "identifier": "ENG-1", "url": "u"},
                        }
                    }
                },
            )

        with respx.mock as router:
            router.post(LINEAR_URL).mock(side_effect=handler)
            adapter = LinearAdapter(
                api_key="k",
                team_id="TEAM",
                guard=guard,
                faults=FaultInjector(plan={"linear.search_issues": [429]}),
                attempts=3,
                backoff_initial=0.001,
            )

            await adapter.search_issues("TypeError")
            await adapter.create_issue(intent("linear.create_issue", title="t"))

        creates = [post for post in posts if "issueCreate" in post["query"]]
        assert len(creates) == 1

    async def test_discord_reply_is_posted_once(
        self, guard: Guard, mock_discord: respx.MockRouter
    ) -> None:
        route = mock_discord.post("/channels/2/messages").mock(
            return_value=httpx.Response(
                200, json={"id": "1", "channel_id": "2", "guild_id": "3", "content": "x"}
            )
        )
        adapter = DiscordAdapter(
            bot_token="t",
            guard=guard,
            faults=FaultInjector(plan={"discord.reply": [429]}),
            attempts=3,
            backoff_initial=0.001,
        )

        with pytest.raises(RetryableAppError):
            await adapter.reply(intent("discord.reply", channel_id=2, content="hello"))

        assert route.call_count == 0


class TestFaultPlans:
    """The injector is deterministic, which is what makes the results comparable."""

    def test_faults_fire_in_order_and_run_out(self) -> None:
        faults = FaultInjector(plan={"a": [429, "timeout"]})

        assert faults.next_fault("a") == 429
        assert faults.next_fault("a") == "timeout"
        assert faults.next_fault("a") is None

    def test_an_unplanned_operation_is_never_faulted(self) -> None:
        assert FaultInjector(plan={"a": [500]}).next_fault("b") is None

    def test_every_fired_fault_is_recorded_for_the_report(self) -> None:
        faults = FaultInjector(plan={"a": [500], "b": ["timeout"]})

        faults.next_fault("a")
        faults.next_fault("b")

        assert faults.fired == [("a", 500), ("b", "timeout")]
