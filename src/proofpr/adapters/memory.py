"""In-memory applications.

They exist so ProofPR can run with no credentials and no network: `proofpr run
--dry-run` uses them, the evaluation smoke job in CI uses them, and so does every
unit test.

They are not mocks. Each keeps real state, enforces the rules that matter (a
marker must be present for readback to pass, a message cannot be edited before it
is created), and goes through the same guard as the real adapters, so a test that
passes here is evidence about the guarded path rather than about a stub.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from proofpr.adapters.idempotency import carries
from proofpr.domain.errors import PermanentAppError, VerificationError
from proofpr.domain.models import HealthCheck, UntrustedText, WriteIntent, WriteResult
from proofpr.guard import Guard


@dataclass(frozen=True, slots=True)
class Fault:
    """One planned failure of one write.

    `after_apply` is the case that matters most and is hardest to see: the
    application made the change, and the response never arrived. A retry that
    does not look first creates the thing twice.
    """

    error: Exception
    after_apply: bool = False


WriteMethod = Callable[[Any, WriteIntent], Awaitable[WriteResult]]


def _losing_response(method: WriteMethod, operation: str) -> WriteMethod:
    """Wrap a write so a planned after-apply fault fires once the change is made."""

    @functools.wraps(method)
    async def wrapper(self: FakeApp, intent: WriteIntent) -> WriteResult:
        result = await method(self, intent)
        queue = self.faults.get(operation)
        if queue and queue[0].after_apply:
            fault = queue.pop(0)
            self.fired.append((operation, fault))
            raise fault.error
        return result

    wrapper.__proofpr_loses_responses__ = True  # type: ignore[attr-defined]
    return wrapper


class FakeApp:
    """Shared behaviour: guarded writes, recorded calls, optional faults."""

    app: ClassVar[str] = "fake"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:  # noqa: ANN401
        """Give every write method the ability to lose its response."""
        super().__init_subclass__(**kwargs)
        for name in cls.WRITE_OPERATIONS:
            method = cls.__dict__.get(name)
            if method is not None and not hasattr(method, "__proofpr_loses_responses__"):
                setattr(cls, name, _losing_response(method, f"{cls.app}.{name}"))

    def __init__(self, guard: Guard) -> None:
        """Build a fake bound to a guard."""
        self._guard = guard
        self._ids = itertools.count(1)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_next: dict[str, Exception] = {}
        #: Planned failures per qualified operation, consumed in order.
        self.faults: dict[str, list[Fault]] = {}
        #: Every fault that fired, in order.
        self.fired: list[tuple[str, Fault]] = []

    def inject(
        self, operation: str, error: Exception, *, after_apply: bool = False, times: int = 1
    ) -> None:
        """Plan failures for an operation.

        Args:
            operation: Qualified operation, for example ``linear.create_issue``.
            error: What to raise.
            after_apply: Raise after the change is made, as a lost response.
            times: How many consecutive attempts fail this way.
        """
        self.faults.setdefault(operation, []).extend(
            Fault(error, after_apply=after_apply) for _ in range(times)
        )

    def _write(self, intent: WriteIntent) -> str:
        """Authorize, record, and allocate an identifier for a write."""
        self._guard.authorize(intent)
        self.calls.append((intent.operation, dict(intent.payload)))
        if error := self.fail_next.pop(intent.operation, None):
            raise error
        queue = self.faults.get(intent.operation)
        if queue and not queue[0].after_apply:
            fault = queue.pop(0)
            self.fired.append((intent.operation, fault))
            raise fault.error
        return str(next(self._ids))

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Return None: idempotent operations need no lookup."""
        del intent
        return None

    async def health(self) -> list[HealthCheck]:
        """Report healthy without touching anything."""
        return [HealthCheck(app=self.app, check="fake", ok=True, detail="in-memory")]


class FakeDiscord(FakeApp):
    """Messages and reactions, kept in a dict."""

    app: ClassVar[str] = "discord"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"reply", "edit_status_message", "add_reaction"}
    )

    def __init__(self, guard: Guard) -> None:
        """Build the fake with an empty channel."""
        super().__init__(guard)
        self.messages: dict[str, dict[str, Any]] = {}

    def seed_message(self, channel_id: int, content: str) -> str:
        """Insert an inbound message, as if a user had posted it."""
        message_id = str(next(self._ids))
        self.messages[message_id] = {
            "id": message_id,
            "channel_id": str(channel_id),
            "content": content,
            "author": {"id": "user"},
            "reactions": [],
            "mentions": [],
        }
        return message_id

    async def get_message(self, channel_id: int, message_id: int) -> dict[str, Any]:
        """Return the raw message object, for readback and verification.

        The channel is checked rather than ignored: a fake that returns a message
        from the wrong channel would make the verification step pass on drift it
        is supposed to catch.
        """
        message = self.messages[str(message_id)]
        if message["channel_id"] != str(channel_id):
            raise PermanentAppError("message is in another channel", app=self.app, status=404)
        return message

    async def fetch_message(self, channel_id: int, message_id: int) -> UntrustedText:
        """Return a seeded message as untrusted text."""
        message = self.messages[str(message_id)]
        return UntrustedText(
            source=f"discord.message.{channel_id}.{message_id}", value=message["content"]
        )

    async def reply(self, intent: WriteIntent) -> WriteResult:
        """Post a message."""
        message_id = self._write(intent)
        self.messages[message_id] = {
            "id": message_id,
            "channel_id": str(intent.payload["channel_id"]),
            "content": f"{intent.payload['content']}\n\n`{intent.marker}`",
            "author": {"id": "bot"},
            "reactions": [],
            "mentions": [],
        }
        return WriteResult(intent=intent, remote_id=message_id, url=f"fake://msg/{message_id}")

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find a reply this bot already posted, ignoring anyone else's messages."""
        if intent.short_operation != "reply":
            return None
        for message in self.messages.values():
            if (
                message["channel_id"] == str(intent.payload["channel_id"])
                and message.get("author", {}).get("id") == "bot"
                and carries(message["content"], intent, str(intent.payload["content"]))
            ):
                return WriteResult(
                    intent=intent, remote_id=message["id"], url=f"fake://msg/{message['id']}"
                )
        return None

    async def edit_status_message(self, intent: WriteIntent) -> WriteResult:
        """Edit a message that must already exist."""
        message_id = str(intent.payload["message_id"])
        if message_id not in self.messages:
            raise PermanentAppError("no such message", app=self.app, status=404)
        self._write(intent)
        self.messages[message_id]["content"] = f"{intent.payload['content']}\n\n`{intent.marker}`"
        return WriteResult(intent=intent, remote_id=message_id, url=f"fake://msg/{message_id}")

    async def add_reaction(self, intent: WriteIntent) -> WriteResult:
        """React to a message."""
        self._write(intent)
        message_id = str(intent.payload["message_id"])
        self.messages[message_id]["reactions"].append({"emoji": {"name": intent.payload["emoji"]}})
        return WriteResult(intent=intent, remote_id=f"{message_id}:{intent.payload['emoji']}")

    async def readback(self, result: WriteResult) -> WriteResult:
        """Confirm the marker, or the reaction, is visible."""
        if result.intent.short_operation == "add_reaction":
            message = self.messages[str(result.intent.payload["message_id"])]
            names = {reaction["emoji"]["name"] for reaction in message["reactions"]}
            if str(result.intent.payload["emoji"]) not in names:
                raise VerificationError("reaction is not visible on the message")
            return result.confirm({"reactions": sorted(names)})
        message = self.messages[result.remote_id]
        if result.intent.marker not in message["content"]:
            raise VerificationError("message does not carry the run marker")
        return result.confirm({"content_length": len(message["content"])})


class FakeGitHub(FakeApp):
    """Branches, files, pull requests, and check runs."""

    app: ClassVar[str] = "github"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"create_branch", "push", "open_pr", "mark_ready", "comment", "delete_branch"}
    )

    def __init__(
        self, guard: Guard, *, default_branch: str = "main", slug: str = "acme/validkit"
    ) -> None:
        """Build the fake with a default branch and no pull requests."""
        super().__init__(guard)
        self.default_branch = default_branch
        self.slug = slug
        self.branches: dict[str, str] = {default_branch: "base-sha"}
        self.files: dict[tuple[str, str], str] = {}
        self.pulls: dict[str, dict[str, Any]] = {}
        self.comments: dict[str, list[dict[str, Any]]] = {}
        self.check_conclusion = "success"
        #: Items returned by `search_issues`, seeded by the caller.
        self.search_results: list[dict[str, Any]] = []
        #: Commits returned by `list_commits_between`, seeded by the caller.
        self.commits: list[dict[str, Any]] = []
        #: Ranges `list_commits_between` was asked about.
        self.compared: list[tuple[str, str]] = []

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Return seeded results, plus any pull request whose body matches.

        Searching the pull requests this fake created matters: the four-way
        consistency check looks for a run marker across GitHub, and a fake that
        could not find its own writes would make that check pass vacuously.
        """
        term = query.split(" in:")[0].lower()
        matches = [
            item
            for item in self.search_results
            if term in item.get("title", "").lower() or term in (item.get("body") or "").lower()
        ]
        matches += [
            {
                "number": pull["number"],
                "title": f"pull request {pull['number']}",
                "body": pull["body"],
                "state": pull["state"],
                "pull_request": {"url": f"fake://pr/{pull['number']}"},
            }
            for pull in self.pulls.values()
            if term in pull["body"].lower()
        ]
        return matches

    async def list_commits_between(self, base: str, head: str) -> list[dict[str, Any]]:
        """Return the seeded commit list, recording the range asked for."""
        self.compared.append((base, head))
        return self.commits

    async def get_check_runs(self, ref: str) -> list[dict[str, Any]]:
        """Return one completed check run with the configured conclusion."""
        return [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": self.check_conclusion,
                "head_sha": ref,
            }
        ]

    async def ci_conclusion(self, ref: str) -> str:
        """Summarise CI for a ref."""
        runs = await self.get_check_runs(ref)
        if any(run["status"] != "completed" for run in runs):
            return "pending"
        return "success" if all(run["conclusion"] == "success" for run in runs) else "failure"

    async def get_pr(self, number: int) -> dict[str, Any]:
        """Return one pull request."""
        return self.pulls[str(number)]

    async def create_branch(self, intent: WriteIntent) -> WriteResult:
        """Create a branch from the default branch."""
        sha = self._write(intent)
        self.branches[str(intent.payload["branch"])] = sha
        return WriteResult(intent=intent, remote_id=sha)

    async def push(self, intent: WriteIntent) -> WriteResult:
        """Write one file to a branch."""
        sha = self._write(intent)
        branch, path = str(intent.payload["branch"]), str(intent.payload["path"])
        if branch not in self.branches:
            raise PermanentAppError("no such branch", app=self.app, status=404)
        self.files[(branch, path)] = str(intent.payload["content"])
        return WriteResult(intent=intent, remote_id=sha, url=f"fake://file/{branch}/{path}")

    async def open_pr(self, intent: WriteIntent) -> WriteResult:
        """Open a draft pull request."""
        number = self._write(intent)
        self.pulls[number] = {
            "number": int(number),
            "draft": True,
            "state": "open",
            "body": f"{intent.payload['body']}\n\n<!-- {intent.marker} -->",
            "head": intent.payload["head"],
            "node_id": f"node-{number}",
        }
        return WriteResult(intent=intent, remote_id=number, url=f"fake://pr/{number}")

    async def mark_ready(self, intent: WriteIntent) -> WriteResult:
        """Take a pull request out of draft."""
        number = str(intent.payload["number"])
        self._write(intent)
        self.pulls[number]["draft"] = False
        return WriteResult(intent=intent, remote_id=number, url=f"fake://pr/{number}")

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on a pull request."""
        comment_id = self._write(intent)
        self.comments.setdefault(str(intent.payload["number"]), []).append(
            {"id": comment_id, "body": f"{intent.payload['body']}\n\n<!-- {intent.marker} -->"}
        )
        return WriteResult(intent=intent, remote_id=comment_id)

    async def delete_branch(self, intent: WriteIntent) -> WriteResult:
        """Delete a branch."""
        self._write(intent)
        self.branches.pop(str(intent.payload["branch"]), None)
        return WriteResult(intent=intent, remote_id=str(intent.payload["branch"]))

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find a branch, file, pull request, or comment this run already made."""
        operation = intent.short_operation
        branch = str(intent.payload.get("branch", ""))
        if operation == "create_branch" and branch in self.branches:
            return WriteResult(intent=intent, remote_id=self.branches[branch])
        if operation == "delete_branch" and branch not in self.branches:
            return WriteResult(intent=intent, remote_id=branch)
        if operation == "push":
            key = (branch, str(intent.payload["path"]))
            if self.files.get(key) == str(intent.payload["content"]):
                return WriteResult(intent=intent, remote_id=f"blob:{branch}:{key[1]}")
        if operation == "open_pr":
            for number, pull in self.pulls.items():
                if pull["head"] == intent.payload["head"] and carries(
                    pull["body"], intent, str(intent.payload["body"])
                ):
                    return WriteResult(intent=intent, remote_id=number, url=f"fake://pr/{number}")
        if operation == "comment":
            for comment in self.comments.get(str(intent.payload["number"]), []):
                if carries(comment["body"], intent, str(intent.payload["body"])):
                    return WriteResult(intent=intent, remote_id=comment["id"])
        return None

    async def readback(self, result: WriteResult) -> WriteResult:
        """Confirm the write landed, with the same rules the real adapter uses."""
        operation = result.intent.short_operation
        if operation == "create_branch":
            if str(result.intent.payload["branch"]) not in self.branches:
                raise VerificationError("branch was not created")
            return result.confirm({"sha": result.remote_id})
        if operation in {"open_pr", "mark_ready"}:
            pull = self.pulls[result.remote_id]
            if pull["draft"] is not (operation == "open_pr"):
                raise VerificationError("draft state does not match the intent")
            if result.intent.marker not in pull["body"]:
                raise VerificationError("pull request body does not carry the run marker")
            return result.confirm({"draft": pull["draft"]})
        if operation == "push":
            key = (str(result.intent.payload["branch"]), str(result.intent.payload["path"]))
            if key not in self.files:
                raise VerificationError("file is not on the branch")
            return result.confirm({"content_sha": result.remote_id})
        if operation == "delete_branch":
            if str(result.intent.payload["branch"]) in self.branches:
                raise VerificationError("branch still exists")
            return result.confirm({"deleted": True})
        return result.confirm({})


class FakeLinear(FakeApp):
    """Issues, comments, and attachments."""

    app: ClassVar[str] = "linear"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"create_issue", "update_issue", "comment", "attach_url"}
    )

    def __init__(self, guard: Guard) -> None:
        """Build the fake with an empty team."""
        super().__init__(guard)
        self.issues: dict[str, dict[str, Any]] = {}

    async def search_issues(self, query: str) -> list[dict[str, Any]]:
        """Return issues whose title or description contains the term."""
        term = query.lower()
        return [
            issue
            for issue in self.issues.values()
            if term in issue["title"].lower() or term in (issue["description"] or "").lower()
        ]

    def _resolve(self, key: str) -> dict[str, Any]:
        """Return an issue by internal id or by human identifier.

        Linear accepts either, and a duplicate found by search is referred to by
        its identifier from then on. A fake that only accepted the internal id
        would fail on exactly the path the real adapter handles.

        Raises:
            PermanentAppError: There is no such issue.
        """
        if key in self.issues:
            return self.issues[key]
        for issue in self.issues.values():
            if issue["identifier"] == key:
                return issue
        raise PermanentAppError(f"no such issue: {key}", app=self.app, status=404)

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Return one issue, by id or identifier."""
        return self._resolve(issue_id)

    async def create_issue(self, intent: WriteIntent) -> WriteResult:
        """File an issue."""
        issue_id = self._write(intent)
        self.issues[issue_id] = {
            "id": issue_id,
            "identifier": f"ENG-{issue_id}",
            "title": intent.payload["title"],
            "description": f"{intent.payload.get('description', '')}\n\n<!-- {intent.marker} -->",
            "url": f"fake://issue/{issue_id}",
            "state": {"name": "Triage", "type": "triage"},
            "comments": {"nodes": []},
            "attachments": {"nodes": []},
        }
        return WriteResult(intent=intent, remote_id=issue_id, url=f"fake://issue/{issue_id}")

    async def update_issue(self, intent: WriteIntent) -> WriteResult:
        """Update an issue this run owns."""
        issue_id = str(intent.payload["issue_id"])
        self._write(intent)
        self._resolve(issue_id).update(
            {key: value for key, value in intent.payload.items() if key in {"title", "description"}}
        )
        return WriteResult(intent=intent, remote_id=issue_id)

    async def comment(self, intent: WriteIntent) -> WriteResult:
        """Comment on an issue."""
        comment_id = self._write(intent)
        issue = self._resolve(str(intent.payload["issue_id"]))
        issue["comments"]["nodes"].append(
            {"id": comment_id, "body": f"{intent.payload['body']}\n\n<!-- {intent.marker} -->"}
        )
        return WriteResult(intent=intent, remote_id=comment_id)

    async def attach_url(self, intent: WriteIntent) -> WriteResult:
        """Attach a link to an issue."""
        attachment_id = self._write(intent)
        issue = self._resolve(str(intent.payload["issue_id"]))
        issue["attachments"]["nodes"].append(
            {"id": attachment_id, "url": intent.payload["url"], "title": "ProofPR"}
        )
        return WriteResult(intent=intent, remote_id=attachment_id, url=str(intent.payload["url"]))

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find an issue, comment, or attachment this run already made."""
        operation = intent.short_operation
        if operation == "create_issue":
            for issue in self.issues.values():
                if carries(
                    issue["description"] or "", intent, str(intent.payload.get("description", ""))
                ):
                    return WriteResult(intent=intent, remote_id=issue["id"], url=issue["url"])
            return None
        if operation == "comment":
            issue = self._resolve(str(intent.payload["issue_id"]))
            for node in issue["comments"]["nodes"]:
                if carries(node["body"], intent, str(intent.payload["body"])):
                    return WriteResult(intent=intent, remote_id=node["id"])
            return None
        if operation == "attach_url":
            issue = self._resolve(str(intent.payload["issue_id"]))
            for node in issue["attachments"]["nodes"]:
                if node["url"] == intent.payload["url"]:
                    return WriteResult(intent=intent, remote_id=node["id"], url=node["url"])
        return None

    async def readback(self, result: WriteResult) -> WriteResult:
        """Confirm the write is visible on the issue."""
        operation = result.intent.short_operation
        issue_id = (
            result.remote_id
            if operation == "create_issue"
            else str(result.intent.payload["issue_id"])
        )
        issue = self._resolve(issue_id)
        if operation == "create_issue":
            if result.intent.marker not in issue["description"]:
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


class FakeSandbox:
    """Returns scripted results instead of starting a container."""

    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        """Build the fake with a queue of results to return in order."""
        self.results = list(results or [])
        self.invocations: list[list[str]] = []
        self.worktrees: list[str] = []
        self.timeouts: list[int | None] = []

    async def run(
        self,
        *,
        worktree: str,
        command: list[str],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Return the next scripted result, recording the invocation."""
        self.invocations.append(command)
        self.worktrees.append(worktree)
        self.timeouts.append(timeout_seconds)
        if self.results:
            return self.results.pop(0)
        return {
            "exit_code": 0,
            "stdout": "1 passed",
            "stderr": "",
            "duration_ms": 10,
            "timed_out": False,
            "argv": command,
        }
