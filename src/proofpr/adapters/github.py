"""GitHub adapter.

Branches, draft pull requests, and the check runs that decide whether a pull
request may leave draft. CI status is always read from the checks API; it is
never inferred from a local test run, because the claim in the proof block is
that CI was green and nothing weaker.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any, ClassVar

import httpx

from proofpr.adapters.base import FaultInjector, HttpAdapter
from proofpr.adapters.idempotency import carries
from proofpr.domain.errors import PermanentAppError, VerificationError
from proofpr.domain.models import HealthCheck, WriteIntent, WriteResult
from proofpr.guard import Guard

API_ROOT = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


class GitHubAdapter(HttpAdapter):
    """REST and GraphQL client scoped to a single repository."""

    app: ClassVar[str] = "github"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"create_branch", "push", "open_pr", "mark_ready", "comment", "delete_branch"}
    )

    def __init__(
        self,
        *,
        token: str,
        owner: str,
        repo: str,
        guard: Guard,
        client: httpx.AsyncClient | None = None,
        faults: FaultInjector | None = None,
        attempts: int = 4,
        backoff_initial: float = 0.5,
        default_branch: str = "main",
    ) -> None:
        """Build the adapter for one repository."""
        self.owner = owner
        self.repo = repo
        self.default_branch = default_branch
        super().__init__(
            client=client
            or httpx.AsyncClient(
                base_url=API_ROOT,
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": ACCEPT,
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": "proofpr",
                },
            ),
            guard=guard,
            faults=faults,
            attempts=attempts,
            backoff_initial=backoff_initial,
        )

    @property
    def slug(self) -> str:
        """Return ``owner/repo``."""
        return f"{self.owner}/{self.repo}"

    def _repo_url(self, suffix: str) -> str:
        """Build a repository-scoped API path."""
        return f"/repos/{self.owner}/{self.repo}{suffix}"

    # -- reads ----------------------------------------------------------------

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Search issues and pull requests in this repository only."""
        response = await self.request(
            "GET",
            "/search/issues",
            operation="github.search_issues",
            params={"q": f"repo:{self.slug} {query}", "per_page": 20},
        )
        items: list[dict[str, Any]] = response.json().get("items", [])
        return items

    async def list_commits_between(self, base: str, head: str) -> list[dict[str, Any]]:
        """Compare two refs, used to decide whether a bug is already fixed.

        A base the repository does not have (a version with no matching tag, or a
        project that tags nothing) is not an error: it means "already fixed" cannot
        be shown, so no commits are returned and the run carries on.
        """
        try:
            response = await self.request(
                "GET",
                self._repo_url(f"/compare/{base}...{head}"),
                operation="github.compare",
            )
        except PermanentAppError as error:
            if error.status == 404:
                return []
            raise
        commits: list[dict[str, Any]] = response.json().get("commits", [])
        return commits

    async def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        """Return check runs for a ref."""
        response = await self.request(
            "GET",
            self._repo_url(f"/commits/{ref}/check-runs"),
            operation="github.get_check_runs",
        )
        runs: list[dict[str, Any]] = response.json().get("check_runs", [])
        return runs

    async def ci_conclusion(self, ref: str) -> str:
        """Summarise CI for a ref as ``pending``, ``success``, or ``failure``.

        Absence of check runs is reported as ``pending``, never as success. A
        repository with no CI configured therefore never produces a ready pull
        request, which is the safe reading.
        """
        runs = await self.get_check_runs(ref)
        if not runs:
            return "pending"
        if any(run.get("status") != "completed" for run in runs):
            return "pending"
        if all(run.get("conclusion") in {"success", "neutral", "skipped"} for run in runs):
            return "success"
        return "failure"

    async def get_ref_sha(self, branch: str) -> str:
        """Return the commit SHA a branch points at."""
        response = await self.request(
            "GET", self._repo_url(f"/git/ref/heads/{branch}"), operation="github.get_ref"
        )
        return str(response.json()["object"]["sha"])

    async def get_branch(self, branch: str) -> dict[str, Any] | None:
        """Return a branch, or None when it does not exist."""
        try:
            response = await self.request(
                "GET", self._repo_url(f"/branches/{branch}"), operation="github.get_branch"
            )
        except PermanentAppError as error:
            if error.status == httpx.codes.NOT_FOUND:
                return None
            raise
        payload: dict[str, Any] = response.json()
        return payload

    async def get_pr(self, number: int) -> dict[str, Any]:
        """Return one pull request."""
        response = await self.request(
            "GET", self._repo_url(f"/pulls/{number}"), operation="github.get_pr"
        )
        payload: dict[str, Any] = response.json()
        return payload

    # -- writes ---------------------------------------------------------------

    async def create_branch(self, intent: WriteIntent) -> WriteResult:
        """Create this run's branch from the default branch."""

        async def perform() -> tuple[str, str | None]:
            branch = str(intent.payload["branch"])
            base_sha = await self.get_ref_sha(str(intent.payload.get("from", self.default_branch)))
            response = await self.request(
                "POST",
                self._repo_url("/git/refs"),
                operation="github.create_branch",
                retry=False,
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            return str(response.json()["object"]["sha"]), None

        return await self.guarded_write(intent, perform)

    async def push(self, intent: WriteIntent) -> WriteResult:
        """Write file contents to this run's branch, one commit per call."""

        async def perform() -> tuple[str, str | None]:
            branch = str(intent.payload["branch"])
            path = str(intent.payload["path"])
            content = str(intent.payload["content"])
            message = str(intent.payload["message"])
            existing = await self._file_sha(path, branch)
            body: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if existing:
                body["sha"] = existing
            response = await self.request(
                "PUT",
                self._repo_url(f"/contents/{path}"),
                operation="github.push",
                retry=False,
                json=body,
            )
            payload = response.json()
            return str(payload["commit"]["sha"]), str(payload["content"]["html_url"])

        return await self.guarded_write(intent, perform)

    async def open_pr(self, intent: WriteIntent) -> WriteResult:
        """Open a draft pull request carrying the run marker in its body."""

        async def perform() -> tuple[str, str | None]:
            response = await self.request(
                "POST",
                self._repo_url("/pulls"),
                operation="github.open_pr",
                retry=False,
                json={
                    "title": intent.payload["title"],
                    "head": intent.payload["head"],
                    "base": intent.payload.get("base", self.default_branch),
                    "body": self.with_marker(str(intent.payload["body"]), intent),
                    "draft": True,
                },
            )
            payload = response.json()
            return str(payload["number"]), str(payload["html_url"])

        return await self.guarded_write(intent, perform)

    async def mark_ready(self, intent: WriteIntent) -> WriteResult:
        """Take this run's pull request out of draft.

        REST cannot undraft a pull request, so this is the one GraphQL call in
        the adapter.
        """

        async def perform() -> tuple[str, str | None]:
            number = int(intent.payload["number"])
            pr = await self.get_pr(number)
            mutation = (
                "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                "{pullRequest{number isDraft url}}}"
            )
            response = await self.request(
                "POST",
                "/graphql",
                operation="github.mark_ready",
                retry=False,
                json={"query": mutation, "variables": {"id": pr["node_id"]}},
            )
            payload = response.json()
            if "errors" in payload:
                raise PermanentAppError(
                    f"mark_ready failed: {payload['errors']}", app=self.app, status=200
                )
            result = payload["data"]["markPullRequestReadyForReview"]["pullRequest"]
            return str(result["number"]), str(result["url"])

        return await self.guarded_write(intent, perform)

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on this run's pull request."""

        async def perform() -> tuple[str, str | None]:
            number = int(intent.payload["number"])
            response = await self.request(
                "POST",
                self._repo_url(f"/issues/{number}/comments"),
                operation="github.comment",
                retry=False,
                json={"body": self.with_marker(str(intent.payload["body"]), intent)},
            )
            payload = response.json()
            return str(payload["id"]), str(payload["html_url"])

        return await self.guarded_write(intent, perform)

    async def delete_branch(self, intent: WriteIntent) -> WriteResult:
        """Delete this run's own branch. Used to clean up after `doctor`."""

        async def perform() -> tuple[str, str | None]:
            branch = str(intent.payload["branch"])
            await self.request(
                "DELETE",
                self._repo_url(f"/git/refs/heads/{branch}"),
                operation="github.delete_branch",
                retry=False,
            )
            return branch, None

        return await self.guarded_write(intent, perform)

    # -- idempotency ----------------------------------------------------------

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find a branch, file, pull request, or comment this run already made.

        Marking ready is idempotent and is not looked for.
        """
        operation = intent.short_operation

        if operation == "create_branch":
            branch = await self.get_branch(str(intent.payload["branch"]))
            return (
                WriteResult(intent=intent, remote_id=str(branch["commit"]["sha"]))
                if branch
                else None
            )

        if operation == "delete_branch":
            if await self.get_branch(str(intent.payload["branch"])) is None:
                return WriteResult(intent=intent, remote_id=str(intent.payload["branch"]))
            return None

        if operation == "push":
            # The contents API reports a blob sha, which is a pure function of
            # the bytes. Equal shas mean the file on the branch is this write.
            sha = await self._file_sha(str(intent.payload["path"]), str(intent.payload["branch"]))
            if sha is not None and sha == git_blob_sha(str(intent.payload["content"])):
                return WriteResult(intent=intent, remote_id=sha)
            return None

        if operation == "open_pr":
            response = await self.request(
                "GET",
                self._repo_url("/pulls"),
                operation="github.list_pulls",
                params={"head": f"{self.owner}:{intent.payload['head']}", "state": "all"},
            )
            for pull in response.json():
                if carries(pull.get("body") or "", intent, str(intent.payload["body"])):
                    return WriteResult(
                        intent=intent, remote_id=str(pull["number"]), url=str(pull["html_url"])
                    )
            return None

        if operation == "comment":
            response = await self.request(
                "GET",
                self._repo_url(f"/issues/{int(intent.payload['number'])}/comments"),
                operation="github.list_comments",
                params={"per_page": 100},
            )
            for comment in response.json():
                if carries(comment.get("body") or "", intent, str(intent.payload["body"])):
                    return WriteResult(
                        intent=intent,
                        remote_id=str(comment["id"]),
                        url=str(comment["html_url"]),
                    )
            return None

        return None

    # -- verification ---------------------------------------------------------

    async def readback(self, result: WriteResult) -> WriteResult:
        """Re-read a write and confirm the application agrees with the intent."""
        operation = result.intent.short_operation

        if operation == "create_branch":
            branch = await self.get_branch(str(result.intent.payload["branch"]))
            if branch is None:
                raise VerificationError(f"branch {result.intent.payload['branch']} was not created")
            return result.confirm({"sha": branch["commit"]["sha"]})

        if operation in {"open_pr", "mark_ready"}:
            pr = await self.get_pr(int(result.remote_id))
            expected_draft = operation == "open_pr"
            if bool(pr["draft"]) is not expected_draft:
                raise VerificationError(
                    f"pull request {result.remote_id} draft is {pr['draft']}, "
                    f"expected {expected_draft}"
                )
            if result.intent.marker not in (pr.get("body") or ""):
                raise VerificationError("pull request body does not carry the run marker")
            return result.confirm({"draft": pr["draft"], "state": pr["state"]})

        if operation == "push":
            sha = await self._file_sha(
                str(result.intent.payload["path"]), str(result.intent.payload["branch"])
            )
            if sha is None:
                raise VerificationError(f"{result.intent.payload['path']} is not on the branch")
            return result.confirm({"content_sha": sha})

        if operation == "delete_branch":
            if await self.get_branch(str(result.intent.payload["branch"])) is not None:
                raise VerificationError("branch still exists after deletion")
            return result.confirm({"deleted": True})

        return result.confirm({})

    async def _file_sha(self, path: str, branch: str) -> str | None:
        """Return the blob SHA of a file on a branch, or None when absent."""
        try:
            response = await self.request(
                "GET",
                self._repo_url(f"/contents/{path}"),
                operation="github.get_contents",
                params={"ref": branch},
            )
        except PermanentAppError as error:
            if error.status == httpx.codes.NOT_FOUND:
                return None
            raise
        return str(response.json()["sha"])

    # -- health ---------------------------------------------------------------

    async def health(self) -> list[HealthCheck]:
        """Read the repository, then create and delete a throwaway branch.

        The write matters: a token that can read a repository but not push to it
        looks healthy under a read-only check and fails at the moment it counts.
        """
        checks: list[HealthCheck] = []
        started = time.monotonic()
        try:
            response = await self.request(
                "GET", self._repo_url(""), operation="github.get_repo", retry=False
            )
            repo = response.json()
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="read repository",
                    ok=True,
                    detail=f"{self.slug}, default branch {repo['default_branch']}",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
            self.default_branch = str(repo["default_branch"])
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            return [HealthCheck(app=self.app, check="read repository", ok=False, detail=str(error))]

        branch = f"{self._guard.branch_prefix}doctor-{int(time.time())}"
        create = WriteIntent(
            run_id="doctor",
            app="github",
            operation="github.create_branch",
            target=f"{self.slug}@{branch}",
            payload={"branch": branch, "from": self.default_branch},
        )
        started = time.monotonic()
        try:
            written = await self.readback(await self.create_branch(create))
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back a branch",
                    ok=written.verified,
                    detail=branch,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            checks.append(
                HealthCheck(
                    app=self.app, check="write and read back a branch", ok=False, detail=str(error)
                )
            )
            return checks

        cleanup = create.model_copy(
            update={"operation": "github.delete_branch", "payload": {"branch": branch}}
        )
        try:
            await self.readback(await self.delete_branch(cleanup))
            checks.append(
                HealthCheck(app=self.app, check="clean up the branch", ok=True, detail=branch)
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="clean up the branch",
                    ok=False,
                    detail=f"{branch} left behind: {error}",
                )
            )
        return checks


def git_blob_sha(content: str) -> str:
    """Return the sha git assigns to a file with this content."""
    data = content.encode("utf-8")
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()
