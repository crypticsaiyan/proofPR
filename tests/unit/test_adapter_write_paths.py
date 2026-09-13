"""Each adapter's write path, against a mocked API.

These cover the parts that only appear when a real request is built: GraphQL
errors arriving with HTTP 200, base64 file contents, branch creation resolving a
base SHA first, and notes reading back by marker.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from proofpr.adapters.github import API_ROOT as GITHUB_ROOT
from proofpr.adapters.github import GitHubAdapter
from proofpr.adapters.linear import API_URL as LINEAR_URL
from proofpr.adapters.linear import LinearAdapter
from proofpr.domain.errors import PermanentAppError, VerificationError
from proofpr.domain.models import WriteIntent
from proofpr.guard import Guard


def intent(operation: str, **payload: object) -> WriteIntent:
    """Build a write intent for one operation."""
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


def github(guard: Guard) -> GitHubAdapter:
    """Build a GitHub adapter with a fast retry policy."""
    return GitHubAdapter(
        token="t", owner="acme", repo="validkit", guard=guard, attempts=1, backoff_initial=0.001
    )


def linear(guard: Guard) -> LinearAdapter:
    """Build a Linear adapter with a fast retry policy."""
    return LinearAdapter(
        api_key="k", team_id="TEAM", guard=guard, attempts=1, backoff_initial=0.001
    )


class TestGitHubWrites:
    """Branch creation resolves a base, and pushes send base64 content."""

    async def test_creating_a_branch_resolves_the_base_sha_first(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/git/ref/heads/main").mock(
            return_value=httpx.Response(200, json={"object": {"sha": "base-sha"}})
        )
        created: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            created.update(json.loads(request.content))
            return httpx.Response(201, json={"object": {"sha": "new-sha"}})

        mock_github.post("/repos/acme/validkit/git/refs").mock(side_effect=handler)

        result = await github(guard).create_branch(
            intent("github.create_branch", branch="proofpr/r-test")
        )

        assert created == {"ref": "refs/heads/proofpr/r-test", "sha": "base-sha"}
        assert result.remote_id == "new-sha"
        assert result.verified is False

    async def test_pushing_sends_base64_content_and_the_existing_blob_sha(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/contents/src/validkit/parse.py").mock(
            return_value=httpx.Response(200, json={"sha": "old-blob"})
        )
        sent: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={"commit": {"sha": "c1"}, "content": {"html_url": "u"}})

        mock_github.put("/repos/acme/validkit/contents/src/validkit/parse.py").mock(
            side_effect=handler
        )

        await github(guard).push(
            intent(
                "github.push",
                branch="proofpr/r-test",
                path="src/validkit/parse.py",
                content="def parse(): ...",
                message="fix: validate month",
            )
        )

        assert base64.b64decode(str(sent["content"])).decode() == "def parse(): ..."
        assert sent["sha"] == "old-blob"

    async def test_a_new_file_is_pushed_without_a_blob_sha(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/contents/tests/test_new.py").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        sent: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(201, json={"commit": {"sha": "c1"}, "content": {"html_url": "u"}})

        mock_github.put("/repos/acme/validkit/contents/tests/test_new.py").mock(side_effect=handler)

        await github(guard).push(
            intent(
                "github.push",
                branch="proofpr/r-test",
                path="tests/test_new.py",
                content="def test(): ...",
                message="test: add repro",
            )
        )

        assert "sha" not in sent

    async def test_a_missing_branch_reads_back_as_a_verification_failure(
        self, guard: Guard, mock_github: respx.MockRouter
    ) -> None:
        mock_github.get("/repos/acme/validkit/branches/proofpr/r-test").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        from proofpr.domain.models import WriteResult

        result = WriteResult(
            intent=intent("github.create_branch", branch="proofpr/r-test"), remote_id="sha"
        )

        with pytest.raises(VerificationError, match="was not created"):
            await github(guard).readback(result)


class TestLinearWrites:
    """GraphQL reports failure with HTTP 200, so the body is what decides."""

    async def test_graphql_errors_are_permanent_even_though_the_status_is_200(
        self, guard: Guard
    ) -> None:
        with respx.mock as router:
            router.post(LINEAR_URL).mock(
                return_value=httpx.Response(200, json={"errors": [{"message": "team not found"}]})
            )

            with pytest.raises(PermanentAppError, match="team not found"):
                await linear(guard).create_issue(
                    intent("linear.create_issue", title="crash on month=13")
                )

    async def test_an_issue_carries_the_run_marker_in_its_description(self, guard: Guard) -> None:
        sent: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
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
            await linear(guard).create_issue(
                intent("linear.create_issue", title="t", description="body")
            )

        description = sent["variables"]["input"]["description"]  # type: ignore[index]
        assert "proofpr-run:r-test" in description

    async def test_a_comment_that_is_not_visible_fails_verification(self, guard: Guard) -> None:
        from proofpr.domain.models import WriteResult

        with respx.mock as router:
            router.post(LINEAR_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "issue": {
                                "id": "i1",
                                "identifier": "ENG-1",
                                "description": "",
                                "url": "u",
                                "state": {"name": "Triage", "type": "triage"},
                                "comments": {"nodes": []},
                                "attachments": {"nodes": []},
                            }
                        }
                    },
                )
            )
            result = WriteResult(
                intent=intent("linear.comment", issue_id="i1", body="hello"), remote_id="c1"
            )

            with pytest.raises(VerificationError, match="not visible"):
                await linear(guard).readback(result)
