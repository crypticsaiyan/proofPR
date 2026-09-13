"""Discord adapter.

Writes go over the REST API rather than the gateway, so every message this agent
sends passes through the same guarded path as every other write and can be read
back by identifier. The gateway client in :mod:`proofpr.bot` handles intake and
interactions only; it never writes.

Every outbound message sets ``allowed_mentions`` to nothing. A bug report that
says "@everyone" must not become the agent pinging a server.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx

from proofpr.adapters.base import FaultInjector, HttpAdapter
from proofpr.adapters.idempotency import carries
from proofpr.domain.errors import VerificationError
from proofpr.domain.models import HealthCheck, UntrustedText, WriteIntent, WriteResult
from proofpr.guard import Guard

API_ROOT = "https://discord.com/api/v10"

#: Suppresses every mention type, including replies.
NO_MENTIONS: dict[str, Any] = {"parse": [], "replied_user": False}


class DiscordAdapter(HttpAdapter):
    """REST client for one guild."""

    app: ClassVar[str] = "discord"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"reply", "edit_status_message", "add_reaction"}
    )

    def __init__(
        self,
        *,
        bot_token: str,
        guard: Guard,
        client: httpx.AsyncClient | None = None,
        faults: FaultInjector | None = None,
        attempts: int = 4,
        backoff_initial: float = 0.5,
        health_channel_id: int | None = None,
    ) -> None:
        """Build the adapter.

        Args:
            bot_token: Bot token, sent as an ``Bot`` authorization header.
            guard: Allowlist and egress scanner.
            client: Optional preconfigured client.
            faults: Optional fault injector.
            attempts: Total attempts per call, including the first.
            backoff_initial: First backoff delay in seconds.
            health_channel_id: Channel `doctor` writes its check message into.
        """
        self.health_channel_id = health_channel_id
        self._bot_user_id: str | None = None
        super().__init__(
            client=client
            or httpx.AsyncClient(
                base_url=API_ROOT,
                timeout=httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0),
                headers={
                    "Authorization": f"Bot {bot_token}",
                    "User-Agent": "DiscordBot (https://github.com/OWNER/proofpr, 0.1)",
                },
            ),
            guard=guard,
            faults=faults,
            attempts=attempts,
            backoff_initial=backoff_initial,
        )

    # -- reads ----------------------------------------------------------------

    async def fetch_message(self, channel_id: int, message_id: int) -> UntrustedText:
        """Fetch a message body as untrusted text.

        The return type is deliberate: a raw ``str`` could be interpolated into a
        prompt by accident, and this one cannot without going through the
        sanitizer first.
        """
        response = await self.request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            operation="discord.fetch_message",
        )
        payload = response.json()
        return UntrustedText(
            source=f"discord.message.{channel_id}.{message_id}",
            value=str(payload.get("content", "")),
        )

    async def get_message(self, channel_id: int, message_id: int) -> dict[str, Any]:
        """Return the raw message object, for readback."""
        response = await self.request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            operation="discord.get_message",
        )
        payload: dict[str, Any] = response.json()
        return payload

    # -- writes ---------------------------------------------------------------

    async def reply(self, intent: WriteIntent) -> WriteResult:
        """Post a message in this run's thread."""

        async def perform() -> tuple[str, str | None]:
            channel_id = int(intent.payload["channel_id"])
            body: dict[str, Any] = {
                "content": self.with_marker(str(intent.payload["content"]), intent, comment="`{}`"),
                "allowed_mentions": NO_MENTIONS,
            }
            if reply_to := intent.payload.get("reply_to_message_id"):
                body["message_reference"] = {"message_id": str(reply_to)}
            response = await self.request(
                "POST",
                f"/channels/{channel_id}/messages",
                operation="discord.reply",
                retry=False,
                json=body,
            )
            payload = response.json()
            return str(payload["id"]), self._message_url(payload)

        return await self.guarded_write(intent, perform)

    async def edit_status_message(self, intent: WriteIntent) -> WriteResult:
        """Edit this run's own status message in place.

        ``components`` is optional and carries the Approve/Reject/Diff button
        row (AGENTS.md section 2.1) while a run is waiting on a maintainer. An
        edit that clears them (proceeding past approval, or timing out) simply
        omits the key, since Discord treats an absent ``components`` field as
        "leave it alone" only on creation; on an edit it must be sent as an
        empty list to actually remove a previous row, which callers do
        explicitly rather than this method guessing at their intent.
        """

        async def perform() -> tuple[str, str | None]:
            channel_id = int(intent.payload["channel_id"])
            message_id = int(intent.payload["message_id"])
            body: dict[str, Any] = {
                "content": self.with_marker(str(intent.payload["content"]), intent, comment="`{}`"),
                "allowed_mentions": NO_MENTIONS,
            }
            if "components" in intent.payload:
                body["components"] = intent.payload["components"]
            response = await self.request(
                "PATCH",
                f"/channels/{channel_id}/messages/{message_id}",
                operation="discord.edit_status_message",
                json=body,
            )
            payload = response.json()
            return str(payload["id"]), self._message_url(payload)

        return await self.guarded_write(intent, perform)

    async def add_reaction(self, intent: WriteIntent) -> WriteResult:
        """Acknowledge the intake message with a reaction."""

        async def perform() -> tuple[str, str | None]:
            channel_id = int(intent.payload["channel_id"])
            message_id = int(intent.payload["message_id"])
            emoji = str(intent.payload["emoji"])
            await self.request(
                "PUT",
                f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
                operation="discord.add_reaction",
                json=None,
            )
            return f"{message_id}:{emoji}", None

        return await self.guarded_write(intent, perform)

    # -- idempotency ----------------------------------------------------------

    #: How far back a lost reply is looked for. A run's reply lands within
    #: seconds of the attempt, so the newest page is where it would be.
    RECOVERY_WINDOW = 50

    async def find_existing(self, intent: WriteIntent) -> WriteResult | None:
        """Find a reply this run already posted.

        Only messages authored by this bot count. Anyone can type a run marker
        into a channel, and trusting a stranger's message as our own reply would
        let them silence the agent.
        """
        if intent.short_operation != "reply":
            return None
        channel_id = int(intent.payload["channel_id"])
        response = await self.request(
            "GET",
            f"/channels/{channel_id}/messages",
            operation="discord.recent_messages",
            params={"limit": self.RECOVERY_WINDOW},
        )
        bot_id = await self._bot_id()
        for message in response.json():
            if str(message.get("author", {}).get("id")) != bot_id:
                continue
            if carries(str(message.get("content", "")), intent, str(intent.payload["content"])):
                return WriteResult(
                    intent=intent, remote_id=str(message["id"]), url=self._message_url(message)
                )
        return None

    async def _bot_id(self) -> str:
        """Return this bot's user id, fetched once."""
        if self._bot_user_id is None:
            response = await self.request("GET", "/users/@me", operation="discord.me")
            self._bot_user_id = str(response.json()["id"])
        return self._bot_user_id

    # -- verification ---------------------------------------------------------

    async def readback(self, result: WriteResult) -> WriteResult:
        """Re-read the message and confirm the marker is on it."""
        if result.intent.short_operation == "add_reaction":
            message = await self.get_message(
                int(result.intent.payload["channel_id"]),
                int(result.intent.payload["message_id"]),
            )
            emojis = {reaction["emoji"]["name"] for reaction in message.get("reactions", [])}
            if str(result.intent.payload["emoji"]) not in emojis:
                raise VerificationError("reaction is not visible on the message")
            return result.confirm({"reactions": sorted(emojis)})

        message = await self.get_message(
            int(result.intent.payload["channel_id"]), int(result.remote_id)
        )
        if result.intent.marker not in str(message.get("content", "")):
            raise VerificationError("message does not carry the run marker")
        if message.get("mentions") or message.get("mention_everyone"):
            raise VerificationError("message mentions users, which is never permitted")
        return result.confirm({"content_length": len(str(message.get("content", "")))})

    @staticmethod
    def _message_url(payload: dict[str, Any]) -> str:
        """Build a jump link for a message object."""
        guild = payload.get("guild_id", "@me")
        return f"https://discord.com/channels/{guild}/{payload['channel_id']}/{payload['id']}"

    # -- health ---------------------------------------------------------------

    async def health(self) -> list[HealthCheck]:
        """Authenticate, then post a message, read it back, and edit it."""
        checks: list[HealthCheck] = []
        started = time.monotonic()
        try:
            response = await self.request("GET", "/users/@me", operation="discord.me", retry=False)
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="authenticate",
                    ok=True,
                    detail=str(response.json().get("username")),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            return [HealthCheck(app=self.app, check="authenticate", ok=False, detail=str(error))]

        if self.health_channel_id is None:
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back a message",
                    ok=False,
                    detail="set DISCORD_EVAL_CHANNEL_ID or DISCORD_INTAKE_CHANNEL_ID",
                )
            )
            return checks

        intent = WriteIntent(
            run_id="doctor",
            app="discord",
            operation="discord.reply",
            target=str(self.health_channel_id),
            payload={
                "channel_id": self.health_channel_id,
                "content": "ProofPR doctor check.",
            },
        )
        started = time.monotonic()
        try:
            written = await self.readback(await self.reply(intent))
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back a message",
                    ok=written.verified,
                    detail=written.url or written.remote_id,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
        except Exception as error:  # noqa: BLE001 - doctor reports, never raises
            checks.append(
                HealthCheck(
                    app=self.app,
                    check="write and read back a message",
                    ok=False,
                    detail=str(error),
                )
            )
        return checks
