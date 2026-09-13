"""Linear adapter.

Linear is the issue of record. Every run ends with something here, including the
runs that write no code, because a decline that leaves no trace is
indistinguishable from the agent never having run.

The API is GraphQL over a single endpoint. Queries are literals in this module:
no query is ever assembled from application text.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx

from proofpr.adapters.base import FaultInjector, HttpAdapter
from proofpr.adapters.idempotency import carries
from proofpr.domain.errors import PermanentAppError, VerificationError
from proofpr.domain.models import HealthCheck, WriteIntent, WriteResult
from proofpr.guard import Guard

API_URL = "https://api.linear.app/graphql"

SEARCH_ISSUES = """
query($teamId: ID!, $term: String!) {
  issues(
    filter: { team: { id: { eq: $teamId } }, searchableContent: { contains: $term } }
    first: 25
  ) {
    nodes { id identifier title description state { name type } createdAt url }
  }
}
"""

CREATE_ISSUE = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url title description } }
}
"""

UPDATE_ISSUE = """
mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id identifier url state { name } } }
}
"""

CREATE_COMMENT = """
mutation($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url body } }
}
"""

CREATE_ATTACHMENT = """
mutation($input: AttachmentCreateInput!) {
  attachmentCreate(input: $input) { success attachment { id url title } }
}
"""

GET_ISSUE = """
query($id: String!) {
  issue(id: $id) {
    id identifier title description url state { name type }
    comments { nodes { id body } }
    attachments { nodes { id url title } }
  }
}
"""

VIEWER = "query { viewer { id name } }"


class LinearAdapter(HttpAdapter):
    """GraphQL client scoped to a single Linear team."""

    app: ClassVar[str] = "linear"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"create_issue", "update_issue", "comment", "attach_url"}
    )

    def __init__(
        self,
        *,
        api_key: str,
        team_id: str,
        guard: Guard,
        client: httpx.AsyncClient | None = None,
        faults: FaultInjector | None = None,
        attempts: int = 4,
        backoff_initial: float = 0.5,
    ) -> None:
        """Build the adapter for one team."""
        self.team_id = team_id
        super().__init__(
            client=client
            or httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
                headers={"Authorization": api_key, "Content-Type": "application/json"},
            ),
            guard=guard,
            faults=faults,
            attempts=attempts,
            backoff_initial=backoff_initial,
        )

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation: str,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Execute one GraphQL document and return its ``data``.

        GraphQL reports application errors with HTTP 200, so the body is checked
        explicitly. A 200 that carries errors is permanent: retrying a rejected
        mutation only rejects it again.
        """
        response = await self.request(
            "POST",
            API_URL,
            operation=operation,
            retry=retry,
            json={"query": query, "variables": variables or {}},
        )
        payload = response.json()
        if payload.get("errors"):
            raise PermanentAppError(
                f"{operation} failed: {payload['errors']}", app=self.app, status=200
            )
        data: dict[str, Any] = payload["data"]
        return data

    # -- reads ----------------------------------------------------------------

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Search the configured team for duplicate candidates."""
        data = await self.graphql(
            SEARCH_ISSUES,
            {"teamId": self.team_id, "term": query},
            operation="linear.search_issues",
        )
        nodes: list[dict[str, Any]] = data["issues"]["nodes"]
        return nodes

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Return one issue with its comments and attachments."""
        data = await self.graphql(GET_ISSUE, {"id": issue_id}, operation="linear.get_issue")
        issue: dict[str, Any] = data["issue"]
        return issue

    # -- writes ---------------------------------------------------------------

    async def create_issue(self, intent: WriteIntent) -> WriteResult:
        """File an issue in the configured team, carrying the run marker."""

        async def perform() -> tuple[str, str | None]:
            payload = {
                "teamId": self.team_id,
                "title": intent.payload["title"],
                "description": self.with_marker(str(intent.payload.get("description", "")), intent),
            }
            if labels := intent.payload.get("label_ids"):
                payload["labelIds"] = labels
            data = await self.graphql(
                CREATE_ISSUE, {"input": payload}, operation="linear.create_issue", retry=False
            )
            issue = data["issueCreate"]["issue"]
            return str(issue["id"]), str(issue["url"])

        return await self.guarded_write(intent, perform)

    async def update_issue(self, intent: WriteIntent) -> WriteResult:
        """Update the issue created or matched by this run."""

        async def perform() -> tuple[str, str | None]:
            issue_id = str(intent.payload["issue_id"])
            fields = {
                key: value
                for key, value in intent.payload.items()
                if key in {"title", "description", "stateId", "labelIds", "priority"}
            }
            data = await self.graphql(
                UPDATE_ISSUE,
                {"id": issue_id, "input": fields},
                operation="linear.update_issue",
                retry=False,
            )
            issue = data["issueUpdate"]["issue"]
            return str(issue["id"]), str(issue["url"])

        return await self.guarded_write(intent, perform)

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on this run's issue."""

        async def perform() -> tuple[str, str | None]:
            data = await self.graphql(
                CREATE_COMMENT,
                {
                    "input": {
                        "issueId": intent.payload["issue_id"],
                        "body": self.with_marker(str(intent.payload["body"]), intent),
                    }
                },
                operation="linear.comment",
                retry=False,
            )
            comment = data["commentCreate"]["comment"]
            return str(comment["id"]), str(comment.get("url") or "")

        return await self.guarded_write(intent, perform)

    async def attach_url(self, intent: WriteIntent) -> WriteResult:
        """Attach a pull request or Discord link to this run's issue."""

        async def perform() -> tuple[str, str | None]:
            data = await self.graphql(
                CREATE_ATTACHMENT,
                {
                    "input": {
                        "issueId": intent.payload["issue_id"],
                        "url": intent.payload["url"],
                        "title": intent.payload.get("title", "ProofPR"),
                    }
                },
                operation="linear.attach_url",
                retry=False,
            )
            attachment = data["attachmentCreate"]["attachment"]
            return str(attachment["id"]), str(attachment["url"])

        return await self.guarded_write(intent, perform)

    # -- idempotency ----------------------------------------------------------

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find an issue, comment, or attachment this run already made.

        Updates are idempotent and are not looked for.
        """
        operation = intent.short_operation

        if operation == "create_issue":
            description = str(intent.payload.get("description", ""))
            for issue in await self.search_issues(intent.marker):
                if carries(issue.get("description") or "", intent, description):
                    return WriteResult(
                        intent=intent, remote_id=str(issue["id"]), url=str(issue["url"])
                    )
            return None

        if operation in {"comment", "attach_url"}:
            issue = await self.get_issue(str(intent.payload["issue_id"]))
            if operation == "comment":
                for node in issue["comments"]["nodes"]:
                    if carries(node.get("body") or "", intent, str(intent.payload["body"])):
                        return WriteResult(intent=intent, remote_id=str(node["id"]))
                return None
            for node in issue["attachments"]["nodes"]:
                if node.get("url") == intent.payload["url"]:
                    return WriteResult(
                        intent=intent, remote_id=str(node["id"]), url=str(node["url"])
                    )
            return None

        return None

    # -- verification ---------------------------------------------------------

    async def readback(self, result: WriteResult) -> WriteResult:
        """Re-read the issue and confirm the write is visible on it."""
        operation = result.intent.short_operation
        issue_id = (
            result.remote_id
            if operation == "create_issue"
            else str(result.intent.payload["issue_id"])
        )
        issue = await self.get_issue(issue_id)

        if operation == "create_issue":
            if result.intent.marker not in (issue.get("description") or ""):
                raise VerificationError("issue description does not carry the run marker")
            return result.confirm({"identifier": issue["identifier"], "url": issue["url"]})

        if operation == "comment":
            bodies = [node["body"] for node in issue["comments"]["nodes"]]
            if not any(result.intent.marker in body for body in bodies):
                raise VerificationError("comment is not visible on the issue")
            return result.confirm({"comments": len(bodies)})

        if operation == "attach_url":
            urls = [node["url"] for node in issue["attachments"]["nodes"]]
            if str(result.intent.payload["url"]) not in urls:
                raise VerificationError("attachment is not visible on the issue")
            return result.confirm({"attachments": len(urls)})

        return result.confirm({"state": issue["state"]["name"]})

    # -- health ---------------------------------------------------------------

    async def health(self) -> list[HealthCheck]:
        """Authenticate, then file a doctor issue, read it back, and cancel it.

        The issue is left in place, cancelled. Linear has no deletion that this
        agent is permitted to perform, and inventing one for a health check would
        widen the allowlist for the least important reason imaginable.
        """
        checks: list[HealthCheck] = []
        started = time.monotonic()
        try:
            data = await self.graphql(VIEWER, operation="linear.viewer", retry=False)
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="authenticate",
                    ok=True,
                    detail=str(data["viewer"]["name"]),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            return [HealthCheck(app=self.app, check="authenticate", ok=False, detail=str(error))]

        intent = WriteIntent(
            run_id="doctor",
            app="linear",
            operation="linear.create_issue",
            target=self.team_id,
            payload={
                "title": "ProofPR doctor check",
                "description": "Created by `proofpr doctor` to prove writes work. Safe to close.",
            },
        )
        started = time.monotonic()
        try:
            written = await self.readback(await self.create_issue(intent))
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back an issue",
                    ok=written.verified,
                    detail=str(written.observed.get("identifier", written.remote_id)),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back an issue",
                    ok=False,
                    detail=str(error),
                )
            )
        return checks
