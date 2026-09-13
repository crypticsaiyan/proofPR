"""Discord gateway bot: intake and interactions only.

This is the one place the gateway (as opposed to the REST adapter) is used. It
never writes on its own: every message it posts or edits goes through
:class:`~proofpr.adapters.discord.DiscordAdapter`, the same guarded, marker-
carrying, read-back path every other write in this project uses. The bot's job
is to turn two kinds of Discord interaction into a `Report` and a run id, start
the pipeline, and keep one status message current while it works
(AGENTS.md section 2.1):

- The message context menu command "Triage this".
- The slash command ``/triage <link>``, for a message the bot cannot see
  directly (a link pasted from another channel it has access to).

A maintainer's Approve/Reject decision also arrives over the gateway, as a
button click on the status message, and is the only two-way interaction this
module carries: :class:`DiscordApprover` posts the buttons through the guarded
adapter and this module's interaction handler resolves the pending future
`DiscordApprover.request` is waiting on.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import discord
from discord import app_commands

from proofpr.adapters.discord import DiscordAdapter
from proofpr.composition import DISCORD_LINK, Apps, build_apps, build_model, open_ledger
from proofpr.domain.enums import Step
from proofpr.domain.models import WriteIntent
from proofpr.domain.run import Report, RunState
from proofpr.ledger import Ledger, new_run_id
from proofpr.observability.logging import bind_run, clear_run, configure_logging, get_logger
from proofpr.pipeline import Config, Pipeline
from proofpr.settings import Settings, load_settings
from proofpr.steps.approve import Approval, ApprovalRequest

logger = get_logger("proofpr.bot")

#: How long a status message waits for a maintainer before the run treats
#: silence as a refusal. An agent with no one to ask must not publish.
APPROVAL_TIMEOUT_SECONDS = 900.0

#: How often the status message is refreshed while a run is in flight.
STATUS_POLL_SECONDS = 1.5

#: Display phase, in order, per AGENTS.md section 2.1. Several pipeline steps
#: share a user-facing phase, so this is a many-to-one map, not a relabelling of
#: `Step`. Only steps the ledger records as their own `step_started` events can
#: tick a row: test synthesis and the gate run inside `repro_raw`, and the proof
#: checks inside `patch`, so they are named in those rows' labels instead.
PHASES: list[tuple[str, tuple[Step, ...]]] = [
    ("Intake", (Step.SANITIZE, Step.PRE_CHECK, Step.FINGERPRINT)),
    ("Exists", (Step.EXISTS,)),
    ("Worth it", (Step.WORTH_IT,)),
    ("Reproduce + test", (Step.LOCALIZE, Step.REPRO_RAW, Step.CLARIFY)),
    ("Patch + proof", (Step.PATCH,)),
    ("Approval", (Step.APPROVAL,)),
    ("Publish", (Step.PUBLISH,)),
    ("CI", (Step.CI_WAIT,)),
]

#: Outcomes that mean the step the run stopped in went wrong, rather than
#: deciding correctly not to go further (a duplicate or a decline is the
#: product working, and is marked as a stop, not a failure).
FAILED_OUTCOMES = frozenset({"ci_failed", "abandoned", "failed_closed", "confirmed_no_test"})


class DiscordApprover:
    """Asks a maintainer via buttons on the run's own status message.

    Implements the same `ApprovalPort` protocol as `AutoApprover` and
    `DenyingApprover` (`proofpr.steps.approve`), so the pipeline does not know
    or care that this one talks to Discord. The buttons are sent through the
    guarded adapter, like every other write; the click that resolves them
    arrives over the gateway and is delivered here by `on_interaction`, via
    the `pending` registry both share.
    """

    def __init__(
        self,
        *,
        discord_adapter: DiscordAdapter,
        channel_id: int,
        message_id: int,
        pending: dict[str, asyncio.Future[Approval]],
        timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the approver to one run's status message."""
        self._discord = discord_adapter
        self._channel_id = channel_id
        self._message_id = message_id
        self._pending = pending
        self._timeout = timeout_seconds

    async def request(self, request: ApprovalRequest) -> Approval:
        """Post Approve/Reject/Diff buttons and wait for a click or a timeout."""
        future: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._pending[request.run_id] = future
        try:
            await self._discord.edit_status_message(
                WriteIntent(
                    run_id=request.run_id,
                    app="discord",
                    operation="discord.edit_status_message",
                    target=f"{self._channel_id}/{self._message_id}",
                    payload={
                        "channel_id": self._channel_id,
                        "message_id": self._message_id,
                        "content": f"{request.summary}\n\n⏳ Approval    awaiting a maintainer",
                        "components": _approval_components(request.run_id),
                    },
                )
            )
            try:
                return await asyncio.wait_for(future, timeout=self._timeout)
            except TimeoutError:
                return Approval(
                    approved=False,
                    decided_by="timeout",
                    reason=f"no maintainer response within {int(self._timeout)}s",
                )
        finally:
            self._pending.pop(request.run_id, None)
            await self._discord.edit_status_message(
                WriteIntent(
                    run_id=request.run_id,
                    app="discord",
                    operation="discord.edit_status_message",
                    target=f"{self._channel_id}/{self._message_id}",
                    payload={
                        "channel_id": self._channel_id,
                        "message_id": self._message_id,
                        "content": f"{request.summary}\n\n✅ Approval    decided",
                        "components": [],
                    },
                )
            )


def _approval_components(run_id: str) -> list[dict[str, Any]]:
    """Return the raw Discord message-component payload for the button row."""
    return [
        {
            "type": 1,  # action row
            "components": [
                {
                    "type": 2,  # button
                    "style": 3,  # success (green)
                    "label": "Approve PR",
                    "emoji": {"name": "✅"},
                    "custom_id": f"proofpr:approve:{run_id}",
                },
                {
                    "type": 2,
                    "style": 4,  # danger (red)
                    "label": "Reject",
                    "emoji": {"name": "❌"},
                    "custom_id": f"proofpr:reject:{run_id}",
                },
            ],
        }
    ]


def _phase_lines(events: list[dict[str, Any]]) -> list[str]:
    """Render the phase checklist from a run's ledger events so far.

    A row is done when every step of it that actually started has finished, not
    when every step it could contain has: most runs never enter clarify, and a
    row waiting on a step that will never start would spin forever. The row a
    run stopped in is marked as failed or as a deliberate stop.
    """
    started = {event["step"] for event in events if event["kind"] == "step_started"}
    finished = {
        event["step"] for event in events if event["kind"] in ("step_finished", "step_skipped")
    }
    stop = next((event for event in reversed(events) if event["kind"] == "run_stopped"), None)
    stop_step = stop["step"] if stop else None
    stop_outcome = str((stop.get("payload") or {}).get("outcome", "")) if stop else ""

    lines = []
    for label, steps in PHASES:
        values = {step.value for step in steps}
        entered = values & started
        if not entered:
            continue
        if stop_step in values and stop_outcome != "pr_opened":
            icon = "❌" if stop_outcome in FAILED_OUTCOMES else "⏹️"
        elif entered <= finished:
            icon = "✅"
        else:
            icon = "⏳"
        lines.append(f"{icon} {label}")
    return lines


def _status_text(
    run_id: str, events: list[dict[str, Any]], *, cost_usd: float, started: float
) -> str:
    """Render the whole status message, matching AGENTS.md section 2.1."""
    elapsed = int(time.monotonic() - started)
    header = f"🤖 ProofPR · run {run_id}                     ${cost_usd:.2f} · {elapsed}s"
    return "\n".join([header, *_phase_lines(events)])


def _outcome_text(state: RunState) -> str:
    """Render the terminal line appended once a run concludes."""
    outcome = state.outcome.value if state.outcome else "none"
    reason = f" ({state.reason.value})" if state.reason else ""
    lines = [f"**{outcome}**{reason}"]
    if state.linear_url:
        lines.append(f"issue: {state.linear_identifier} {state.linear_url}")
    if state.pr_url:
        lines.append(f"pull request: {state.pr_url}")
    if state.proof_complete:
        lines.append(f"mutants killed: {state.mutants_killed}/{state.mutants_total}")
    if state.injection_flags:
        lines.append(f"⚠️ untrusted instructions ignored: {', '.join(state.injection_flags)}")
    if state.receipt:
        lines.append(f"receipt: `{state.receipt}`")
    return "\n".join(lines)


class ProofPRBot(discord.Client):
    """The gateway client. Intake and interactions only; it never writes."""

    def __init__(self, settings: Settings) -> None:
        """Bind the client to its settings, and prepare the command tree."""
        # No privileged intents. The message a "Triage this" context menu targets
        # arrives with its content in the interaction itself, which Discord
        # delivers without the Message Content intent; requiring it would stop
        # the bot from connecting at all on an app that has not enabled it.
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.pending_approvals: dict[str, asyncio.Future[Approval]] = {}
        # A reference to every in-flight triage task, so none is garbage
        # collected mid-run merely because nothing else held it.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._register_commands()

    async def setup_hook(self) -> None:
        """Sync application commands to the configured guild, if one is set."""
        guild_id = self.settings.discord.guild_id
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    def _register_commands(self) -> None:
        """Register the context menu and the slash command."""

        @self.tree.context_menu(name="Triage this")
        async def triage_context(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            await self._start_triage(interaction, message)

        @self.tree.command(name="triage", description="Triage a bug report by its Discord link.")
        @app_commands.describe(link="A discord.com/channels/... message link.")
        async def triage_command(interaction: discord.Interaction, link: str) -> None:
            match = DISCORD_LINK.fullmatch(link.strip())
            if match is None:
                await interaction.response.send_message(
                    "that doesn't look like a Discord message link.", ephemeral=True
                )
                return
            channel = self.get_channel(int(match.group("channel")))
            if not isinstance(channel, discord.abc.Messageable):
                await interaction.response.send_message("I can't see that channel.", ephemeral=True)
                return
            try:
                message = await channel.fetch_message(int(match.group("message")))
            except discord.NotFound:
                await interaction.response.send_message(
                    "that message doesn't exist, or I can't read it.", ephemeral=True
                )
                return
            await self._start_triage(interaction, message)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Resolve a pending approval when its button is clicked."""
        if interaction.type is not discord.InteractionType.component:
            return
        data: dict[str, Any] = dict(interaction.data or {})
        custom_id = data.get("custom_id", "")
        if not isinstance(custom_id, str) or not custom_id.startswith("proofpr:"):
            return
        _, decision, run_id = custom_id.split(":", 2)
        future = self.pending_approvals.get(run_id)
        await interaction.response.defer()
        if future is None or future.done():
            return
        who = str(interaction.user)
        if decision == "approve":
            future.set_result(Approval(approved=True, decided_by=who))
        else:
            future.set_result(
                Approval(approved=False, decided_by=who, reason="rejected in Discord")
            )

    async def _start_triage(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """Post the initial status message and run the pipeline against it."""
        if not message.content.strip():
            await interaction.response.send_message(
                "I can't read that message's text. Use right-click → Apps → Triage this on "
                "the report itself, or enable the Message Content intent for this bot.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🤖 ProofPR · starting…")
        status = await interaction.original_response()

        report = Report(
            source="discord",
            text=message.content,
            author=str(message.author),
            channel_id=status.channel.id,
            message_id=status.id,
            url=message.jump_url,
        )
        # Not awaited: the interaction handler must return promptly, and the
        # status message this task edits is how its progress is observed.
        self._background_tasks.add(task := asyncio.create_task(self._run_and_report(report)))
        task.add_done_callback(self._background_tasks.discard)

    async def _run_and_report(self, report: Report) -> None:
        """Run one report through the pipeline, keeping the status message live."""
        assert report.channel_id is not None and report.message_id is not None  # noqa: S101
        apps = build_apps(self.settings)
        ledger = open_ledger(self.settings)
        has_model_key = bool(self.settings.models.openrouter_api_key.get_secret_value())
        model = build_model(self.settings) if has_model_key else None
        run_id = new_run_id()
        approver = DiscordApprover(
            discord_adapter=apps.discord,
            channel_id=report.channel_id,
            message_id=report.message_id,
            pending=self.pending_approvals,
        )
        pipeline = Pipeline(
            apps=apps,
            ledger=ledger,
            model=model,
            approver=approver,
            config=Config.from_document(apps.config),
        )

        bind_run(run_id)
        started = time.monotonic()
        task = asyncio.create_task(pipeline.run(report, run_id=run_id))
        try:
            while not task.done():
                await self._refresh_status(apps, ledger, report, run_id, started)
                await asyncio.wait([task], timeout=STATUS_POLL_SECONDS)
            state = task.result()
            await self._refresh_status(apps, ledger, report, run_id, started, final=state)
        except Exception:
            logger.exception("bot_run_failed", run_id=run_id)
            raise
        finally:
            clear_run()
            await apps.aclose()
            ledger.close()

    async def _refresh_status(
        self,
        apps: Apps,
        ledger: Ledger,
        report: Report,
        run_id: str,
        started: float,
        *,
        final: RunState | None = None,
    ) -> None:
        """Edit the status message with the run's current or final state."""
        assert report.channel_id is not None and report.message_id is not None  # noqa: S101
        events = ledger.events(run_id)
        cost = ledger.total_cost(run_id)
        text = _status_text(run_id, events, cost_usd=cost, started=started)
        if final is not None:
            text = f"{text}\n\n{_outcome_text(final)}"
        await apps.discord.edit_status_message(
            WriteIntent(
                run_id=run_id,
                app="discord",
                operation="discord.edit_status_message",
                target=f"{report.channel_id}/{report.message_id}",
                payload={
                    "channel_id": report.channel_id,
                    "message_id": report.message_id,
                    "content": text,
                    # Stated on every refresh, never left implicit: an edit that
                    # omits components keeps whatever the last one set, so a
                    # refresh racing the approver's clean-up could leave dead
                    # Approve/Reject buttons on a finished run.
                    "components": _approval_components(run_id)
                    if final is None and run_id in self.pending_approvals
                    else [],
                },
            )
        )


def main() -> None:
    """Start the bot. Blocks until it is stopped."""
    settings = load_settings()
    configure_logging(settings.log_level)
    token = settings.discord.bot_token.get_secret_value()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set; the bot has nothing to log in with.")
    client = ProofPRBot(settings)
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
