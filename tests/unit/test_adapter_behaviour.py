"""Adapter rules that are about correctness rather than transport."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from proofpr.adapters.discord import NO_MENTIONS, DiscordAdapter
from proofpr.adapters.github import API_ROOT, GitHubAdapter
from proofpr.domain.errors import VerificationError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.guard import Guard


@pytest.fixture
def mock_github() -> Iterator[respx.MockRouter]:
    """Intercept the GitHub API."""
    with respx.mock(base_url=API_ROOT, assert_all_called=False) as router:
        yield router


def github(guard: Guard) -> GitHubAdapter:
    """Build a GitHub adapter for behaviour tests."""
    return GitHubAdapter(
        token="t", owner="acme", repo="validkit", guard=guard, attempts=1, backoff_initial=0.001
    )


class TestCiConclusion:
    """CI status is read, never assumed, and absence is never success."""

    async def test_no_check_runs_is_pending_not_success(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/commits/abc/check-runs").mock(
            return_value=httpx.Response(200, json={"check_runs": []})
        )

        assert await github(guard).ci_conclusion("abc") == "pending"

    async def test_an_incomplete_run_is_pending(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/commits/abc/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"status": "completed", "conclusion": "success"},
                        {"status": "in_progress", "conclusion": None},
                    ]
                },
            )
        )

        assert await github(guard).ci_conclusion("abc") == "pending"

    async def test_any_failure_fails_the_whole_ref(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/commits/abc/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"status": "completed", "conclusion": "success"},
                        {"status": "completed", "conclusion": "failure"},
                    ]
                },
            )
        )

        assert await github(guard).ci_conclusion("abc") == "failure"

    async def test_skipped_and_neutral_count_as_success(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/commits/abc/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"status": "completed", "conclusion": "skipped"},
                        {"status": "completed", "conclusion": "neutral"},
                    ]
                },
            )
        )

        assert await github(guard).ci_conclusion("abc") == "success"


class TestReadback:
    """A write is finished when the application agrees, not when it returns 200."""

    async def test_a_pull_request_without_the_marker_fails_verification(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/pulls/7").mock(
            return_value=httpx.Response(
                200, json={"number": 7, "draft": True, "state": "open", "body": "no marker here"}
            )
        )
        intent = WriteIntent(
            run_id="r-test",
            app="github",
            operation="github.open_pr",
            target="acme/validkit#7",
            payload={"head": "proofpr/r-test", "base": "main", "draft": True, "body": "x"},
        )

        with pytest.raises(VerificationError, match="run marker"):
            await github(guard).readback(WriteResult(intent=intent, remote_id="7"))

    async def test_a_pull_request_in_the_wrong_draft_state_fails_verification(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/pulls/7").mock(
            return_value=httpx.Response(
                200,
                json={
                    "number": 7,
                    "draft": False,
                    "state": "open",
                    "body": "<!-- proofpr-run:r-test -->",
                },
            )
        )
        intent = WriteIntent(
            run_id="r-test",
            app="github",
            operation="github.open_pr",
            target="acme/validkit#7",
            payload={"head": "proofpr/r-test", "base": "main", "draft": True, "body": "x"},
        )

        with pytest.raises(VerificationError, match="draft"):
            await github(guard).readback(WriteResult(intent=intent, remote_id="7"))


class TestDiscordMentions:
    """The agent never pings anyone, whatever a report asks for."""

    async def test_every_message_suppresses_mentions(self, guard: Guard) -> None:
        sent: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "1", "channel_id": "2", "guild_id": "3", "content": "x"}
            )

        with respx.mock(base_url="https://discord.com/api/v10") as router:
            router.post("/channels/2/messages").mock(side_effect=handler)
            adapter = DiscordAdapter(bot_token="t", guard=guard, attempts=1)
            await adapter.reply(
                WriteIntent(
                    run_id="r-test",
                    app="discord",
                    operation="discord.reply",
                    target="2",
                    payload={"channel_id": 2, "content": "@everyone the fix is up"},
                )
            )

        assert sent["allowed_mentions"] == NO_MENTIONS

    async def test_a_message_that_mentions_anyone_fails_verification(self, guard: Guard) -> None:
        with respx.mock(base_url="https://discord.com/api/v10") as router:
            router.get("/channels/2/messages/1").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "1",
                        "content": "hello `proofpr-run:r-test`",
                        "mentions": [{"id": "9"}],
                    },
                )
            )
            adapter = DiscordAdapter(bot_token="t", guard=guard, attempts=1)
            intent = WriteIntent(
                run_id="r-test",
                app="discord",
                operation="discord.reply",
                target="2",
                payload={"channel_id": 2, "content": "hello"},
            )

            with pytest.raises(VerificationError, match="mentions"):
                await adapter.readback(WriteResult(intent=intent, remote_id="1"))


class TestMarkers:
    """Run markers make a retry idempotent, and are added exactly once."""

    def test_a_marker_is_added_and_not_duplicated(self, guard: Guard) -> None:
        intent = WriteIntent(
            run_id="r-7f3a",
            app="github",
            operation="github.comment",
            target="acme/validkit#1",
            payload={},
        )

        once = GitHubAdapter.with_marker("body", intent)
        twice = GitHubAdapter.with_marker(once, intent)

        assert once.count("proofpr-run:r-7f3a") == 1
        assert twice == once
