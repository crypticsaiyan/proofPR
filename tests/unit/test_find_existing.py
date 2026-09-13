"""Finding a write that already landed, against each application's real API shape.

The pipeline trusts `find_existing` to decide whether re-sending a write would
duplicate it. A false negative writes twice; a false positive silently drops a
write. Both directions are tested here for every operation that creates
something.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from proofpr.adapters.discord import API_ROOT as DISCORD_ROOT
from proofpr.adapters.discord import DiscordAdapter
from proofpr.adapters.github import API_ROOT as GITHUB_ROOT
from proofpr.adapters.github import GitHubAdapter, git_blob_sha
from proofpr.adapters.idempotency import carries, normalise
from proofpr.adapters.linear import API_URL as LINEAR_URL
from proofpr.adapters.linear import LinearAdapter
from proofpr.domain.models import WriteIntent
from proofpr.guard import Guard

MARKER = "proofpr-run:r-test"


def intent(operation: str, **payload: object) -> WriteIntent:
    return WriteIntent(
        run_id="r-test",
        app=operation.split(".", 1)[0],
        operation=operation,
        target="target",
        payload=dict(payload),
    )


@pytest.fixture
def mock_github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=GITHUB_ROOT, assert_all_called=False) as router:
        yield router


@pytest.fixture
def mock_discord() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=DISCORD_ROOT, assert_all_called=False) as router:
        yield router


class TestCarries:
    """The match rule itself."""

    def test_the_marker_is_required(self) -> None:
        write = intent("linear.comment", body="Another reporter hit this.")

        assert not carries("Another reporter hit this.", write, "Another reporter hit this.")

    def test_markup_rewrites_do_not_hide_a_write(self) -> None:
        write = intent("linear.comment", body="Receipt `sha256:abc`\n\nEvery step is chained.")
        stored = f"Receipt sha256:abc\n\nEvery **step** is chained.\n\n<!-- {MARKER} -->"

        assert carries(stored, write, str(write.payload["body"]))

    def test_two_writes_by_one_run_are_told_apart(self) -> None:
        receipt = intent("linear.comment", body="Receipt `sha256:abc` for this run.")
        note = f"Another reporter hit this.\n\n<!-- {MARKER} -->"

        assert not carries(note, receipt, str(receipt.payload["body"]))

    def test_normalising_keeps_only_letters_and_digits(self) -> None:
        assert normalise("Hello, **World** 42!") == "helloworld42"


class TestGitHub:
    """Branches, files, pull requests, comments."""

    def adapter(self, guard: Guard) -> GitHubAdapter:
        return GitHubAdapter(token="t", owner="acme", repo="validkit", guard=guard, attempts=1)

    def test_the_blob_sha_matches_git(self) -> None:
        # `printf 'hello\n' | git hash-object --stdin`
        assert git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

    async def test_an_existing_branch_is_found(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/branches/proofpr/r-test").mock(
            return_value=httpx.Response(200, json={"commit": {"sha": "abc"}})
        )

        found = await self.adapter(guard).find_existing(
            intent("github.create_branch", branch="proofpr/r-test")
        )

        assert found is not None
        assert found.remote_id == "abc"

    async def test_a_missing_branch_is_not(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/branches/proofpr/r-test").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        assert (
            await self.adapter(guard).find_existing(
                intent("github.create_branch", branch="proofpr/r-test")
            )
            is None
        )

    async def test_a_push_is_found_only_when_the_content_matches(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/contents/src/a.py").mock(
            return_value=httpx.Response(200, json={"sha": git_blob_sha("x = 1\n")})
        )
        adapter = self.adapter(guard)

        same = await adapter.find_existing(
            intent("github.push", branch="b", path="src/a.py", content="x = 1\n")
        )
        different = await adapter.find_existing(
            intent("github.push", branch="b", path="src/a.py", content="x = 2\n")
        )

        assert same is not None
        assert different is None

    async def test_a_pull_request_is_found_by_head_and_body(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        route = mock_github.get("/repos/acme/validkit/pulls").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "html_url": "https://github.com/acme/validkit/pull/7",
                        "body": f"Fix the month check.\n\n<!-- {MARKER} -->",
                    }
                ],
            )
        )

        found = await self.adapter(guard).find_existing(
            intent("github.open_pr", head="proofpr/r-test", title="t", body="Fix the month check.")
        )

        assert found is not None
        assert found.remote_id == "7"
        assert route.calls.last.request.url.params["head"] == "acme:proofpr/r-test"
        assert route.calls.last.request.url.params["state"] == "all"

    async def test_marking_ready_is_idempotent_and_never_looked_up(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        assert await self.adapter(guard).find_existing(intent("github.mark_ready", number=7)) is (
            None
        )
        assert not mock_github.calls


class TestLinear:
    """Issues by marker, comments by body, attachments by url."""

    def adapter(self, guard: Guard) -> LinearAdapter:
        return LinearAdapter(api_key="k", team_id="TEAM", guard=guard, attempts=1)

    async def test_an_issue_is_found_by_searching_for_the_marker(self, guard: Guard) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.post(LINEAR_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "issues": {
                                "nodes": [
                                    {
                                        "id": "i-1",
                                        "url": "https://linear.app/x/ENG-1",
                                        "description": f"TypeError in parse_date\n\n{MARKER}",
                                    }
                                ]
                            }
                        }
                    },
                )
            )

            found = await self.adapter(guard).find_existing(
                intent("linear.create_issue", title="t", description="TypeError in parse_date")
            )

        assert found is not None
        assert found.remote_id == "i-1"
        sent = json.loads(route.calls.last.request.content)
        assert sent["variables"]["term"] == MARKER

    async def test_a_comment_with_the_same_marker_but_another_body_is_not_found(
        self, guard: Guard
    ) -> None:
        issue = {
            "issue": {
                "id": "i-1",
                "identifier": "ENG-1",
                "title": "t",
                "description": "",
                "url": "u",
                "state": {"name": "Todo"},
                "comments": {
                    "nodes": [{"id": "c-1", "body": f"Another reporter hit this.\n\n{MARKER}"}]
                },
                "attachments": {"nodes": []},
            }
        }
        with respx.mock(assert_all_called=False) as router:
            router.post(LINEAR_URL).mock(return_value=httpx.Response(200, json={"data": issue}))

            found = await self.adapter(guard).find_existing(
                intent("linear.comment", issue_id="i-1", body="Receipt `sha256:abc`")
            )

        assert found is None


class TestDiscord:
    """Replies by this bot only."""

    def adapter(self, guard: Guard) -> DiscordAdapter:
        return DiscordAdapter(bot_token="t", guard=guard, attempts=1)

    async def test_a_reply_by_this_bot_is_found(
        self, guard: Guard, mock_discord: respx.MockRouter
    ) -> None:
        mock_discord.get("/users/@me").mock(return_value=httpx.Response(200, json={"id": "999"}))
        mock_discord.get("/channels/1/messages").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "55",
                        "channel_id": "1",
                        "author": {"id": "999"},
                        "content": f"Filed as ENG-1.\n\n`{MARKER}`",
                    }
                ],
            )
        )

        found = await self.adapter(guard).find_existing(
            intent("discord.reply", channel_id=1, content="Filed as ENG-1.")
        )

        assert found is not None
        assert found.remote_id == "55"

    async def test_a_stranger_pasting_the_marker_cannot_silence_the_reply(
        self, guard: Guard, mock_discord: respx.MockRouter
    ) -> None:
        mock_discord.get("/users/@me").mock(return_value=httpx.Response(200, json={"id": "999"}))
        mock_discord.get("/channels/1/messages").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "56",
                        "channel_id": "1",
                        "author": {"id": "12345"},
                        "content": f"Filed as ENG-1.\n\n`{MARKER}`",
                    }
                ],
            )
        )

        found = await self.adapter(guard).find_existing(
            intent("discord.reply", channel_id=1, content="Filed as ENG-1.")
        )

        assert found is None
