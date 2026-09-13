"""Telling reporters their bug is fixed.

The part of triage that nobody does. A bug report that becomes a pull request
that merges is still, from the reporter's side, a message they sent into a
channel and never heard about again. This closes that loop, and it is the
cheapest genuinely useful thing in the whole system.

Driven by a merge webhook rather than polling: the pull request body carries the
run marker, so a merge event identifies the run that produced it without any
lookup table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from proofpr.domain.enums import Outcome
from proofpr.domain.models import WriteIntent
from proofpr.domain.run import RunState
from proofpr.ledger import Ledger
from proofpr.observability.logging import get_logger
from proofpr.post.reconciler import last_state

logger = get_logger("proofpr.release")

RUN_MARKER = re.compile(r"proofpr-run:(?P<run_id>[\w-]+)")


@dataclass
class Notification:
    """What was done when a fix shipped."""

    run_id: str
    told_reporter: bool = False
    updated_linear: bool = False
    detail: str = ""


def run_id_from_body(body: str | None) -> str | None:
    """Extract the run a pull request belongs to, from its own body."""
    match = RUN_MARKER.search(body or "")
    return match.group("run_id") if match else None


class ReleaseNotifier:
    """Closes the loop when a proof-carrying pull request merges."""

    def __init__(self, *, apps: Any, ledger: Ledger) -> None:  # noqa: ANN401
        """Build the notifier."""
        self.apps = apps
        self.ledger = ledger

    async def on_merged(self, *, run_id: str, merge_ref: str | None = None) -> Notification:
        """Tell everyone the fix shipped, in the places they are waiting.

        Args:
            run_id: The run whose pull request merged.
            merge_ref: The merge commit or release tag, quoted to the reporter so
                they can check for themselves rather than taking our word.

        Returns:
            What was done, per application.
        """
        state = last_state(self.ledger, run_id)
        if state is None:
            return Notification(run_id=run_id, detail="no state recorded for this run")

        self.ledger.append(run_id, "fix_merged", {"ref": merge_ref, "pr": state.pr_number})
        notification = Notification(run_id=run_id)
        shipped = f" in `{merge_ref}`" if merge_ref else ""

        if state.report.channel_id is not None:
            await self._write(
                state,
                self.apps.discord.reply,
                "discord.reply",
                target=str(state.report.channel_id),
                channel_id=state.report.channel_id,
                reply_to_message_id=state.report.message_id,
                content=(
                    f"Your bug is fixed and merged{shipped}: {state.pr_url}. Thanks for the report."
                ),
            )
            notification.told_reporter = True

        if state.linear_issue_id is not None:
            await self._write(
                state,
                self.apps.linear.comment,
                "linear.comment",
                target=str(state.linear_issue_id),
                issue_id=state.linear_issue_id,
                body=f"Merged{shipped}. {state.pr_url}",
            )
            notification.updated_linear = True

        self.ledger.append(
            run_id,
            "release_notified",
            {
                "reporter": notification.told_reporter,
                "linear": notification.updated_linear,
            },
        )
        logger.info("release_notified", run_id=run_id, ref=merge_ref)
        return notification

    async def on_pull_request_event(self, payload: dict[str, Any]) -> Notification | None:
        """Handle a GitHub pull request webhook, if it is a merge of ours.

        Returns None for everything else. A webhook endpoint receives a great
        deal it should ignore, and ignoring it silently is correct.
        """
        if payload.get("action") != "closed":
            return None
        pull = payload.get("pull_request") or {}
        if not pull.get("merged"):
            return None
        run_id = run_id_from_body(pull.get("body"))
        if run_id is None:
            return None
        return await self.on_merged(
            run_id=run_id, merge_ref=str(pull.get("merge_commit_sha", ""))[:7] or None
        )

    async def _write(
        self,
        state: RunState,
        method: Any,  # noqa: ANN401
        operation: str,
        *,
        target: str,
        **payload: Any,  # noqa: ANN401
    ) -> None:
        """Perform one guarded write and read it back."""
        intent = WriteIntent(
            run_id=state.run_id,
            app=operation.split(".", 1)[0],
            operation=operation,
            target=target,
            payload=payload,
        )
        result = await method(intent)
        adapter = getattr(self.apps, intent.app)
        self.ledger.record_write(await adapter.readback(result))


def outcome_allows_notification(outcome: Outcome) -> bool:
    """Return whether a merge notification makes sense for an outcome.

    A run that never opened a pull request has nothing to announce, and a run
    whose pull request was rejected has nothing good to announce.
    """
    return outcome in {Outcome.PR_OPENED, Outcome.CI_FAILED}
