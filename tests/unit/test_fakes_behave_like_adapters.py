"""The fakes must be trustworthy, or every test using them proves nothing.

These tests hold the fakes to the same rules the real adapters follow: writes go
through the guard, readback fails when the marker is missing, and a write to
something that does not exist is an error rather than a quiet success.
"""

from __future__ import annotations

import pytest

from proofpr.domain.errors import GuardBlockedError, PermanentAppError, VerificationError
from proofpr.domain.models import WriteIntent, WriteResult
from proofpr.guard import Guard
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear


def intent(operation: str, **payload: object) -> WriteIntent:
    """Build a write intent for one operation."""
    return WriteIntent(
        run_id="r-test",
        app=operation.split(".", 1)[0],
        operation=operation,
        target="target",
        payload=dict(payload),
    )


async def test_fake_writes_go_through_the_guard(guard: Guard) -> None:
    github = FakeGitHub(guard)

    with pytest.raises(GuardBlockedError):
        await github.create_branch(intent("github.create_branch", branch="main"))

    assert github.calls == []


async def test_a_fake_pull_request_opens_as_a_draft_and_reads_back(guard: Guard) -> None:
    github = FakeGitHub(guard)

    opened = await github.open_pr(
        intent("github.open_pr", draft=True, head="proofpr/r-test", base="main", body="proof")
    )
    verified = await github.readback(opened)

    assert verified.verified is True
    assert verified.observed["draft"] is True


async def test_marking_ready_requires_ci_and_then_changes_the_state(guard: Guard) -> None:
    github = FakeGitHub(guard)
    opened = await github.open_pr(
        intent("github.open_pr", draft=True, head="proofpr/r-test", base="main", body="proof")
    )

    with pytest.raises(GuardBlockedError):
        await github.mark_ready(intent("github.mark_ready", number=opened.remote_id))

    ready = await github.mark_ready(
        intent("github.mark_ready", number=opened.remote_id, ci_success=True)
    )
    assert (await github.readback(ready)).observed["draft"] is False


async def test_pushing_to_a_branch_that_does_not_exist_fails(guard: Guard) -> None:
    github = FakeGitHub(guard)

    with pytest.raises(PermanentAppError):
        await github.push(
            intent(
                "github.push",
                branch="proofpr/missing",
                path="src/validkit/parse.py",
                content="x",
                message="fix",
            )
        )


async def test_readback_fails_when_the_marker_is_missing(guard: Guard) -> None:
    linear = FakeLinear(guard)
    created = await linear.create_issue(intent("linear.create_issue", title="t", description="d"))
    linear.issues[created.remote_id]["description"] = "someone edited this"

    with pytest.raises(VerificationError, match="run marker"):
        await linear.readback(created)


async def test_a_comment_must_be_visible_on_the_issue(guard: Guard) -> None:
    linear = FakeLinear(guard)
    created = await linear.create_issue(intent("linear.create_issue", title="t"))

    commented = await linear.comment(
        intent("linear.comment", issue_id=created.remote_id, body="a reporter joined")
    )

    assert (await linear.readback(commented)).observed["comments"] == 1


async def test_discord_messages_carry_the_marker_and_can_be_edited(guard: Guard) -> None:
    discord = FakeDiscord(guard)

    posted = await discord.reply(intent("discord.reply", channel_id=1, content="working on it"))
    await discord.readback(posted)
    edited = await discord.edit_status_message(
        intent(
            "discord.edit_status_message",
            channel_id=1,
            message_id=posted.remote_id,
            content="done",
        )
    )

    assert (await discord.readback(edited)).verified is True
    assert "done" in discord.messages[posted.remote_id]["content"]


async def test_editing_a_message_that_does_not_exist_fails(guard: Guard) -> None:
    discord = FakeDiscord(guard)

    with pytest.raises(PermanentAppError):
        await discord.edit_status_message(
            intent("discord.edit_status_message", channel_id=1, message_id="999", content="x")
        )


async def test_an_unverified_result_is_not_treated_as_verified(guard: Guard) -> None:
    linear = FakeLinear(guard)
    created = await linear.create_issue(intent("linear.create_issue", title="t"))

    assert isinstance(created, WriteResult)
    assert created.verified is False
    assert (await linear.readback(created)).verified is True


async def test_an_issue_can_be_addressed_by_its_human_identifier(guard: Guard) -> None:
    # Linear accepts either the internal id or the identifier, and a duplicate
    # found by search is referred to by its identifier from then on. A fake that
    # only knew internal ids crashed the duplicate path and nothing else.
    linear = FakeLinear(guard)
    created = await linear.create_issue(intent("linear.create_issue", title="t"))
    identifier = (await linear.readback(created)).observed["identifier"]

    commented = await linear.comment(
        intent("linear.comment", issue_id=identifier, body="seen this before")
    )

    assert (await linear.get_issue(identifier))["identifier"] == identifier
    assert (await linear.readback(commented)).observed["comments"] == 1


async def test_an_unknown_issue_is_a_permanent_error_not_a_key_error(guard: Guard) -> None:
    linear = FakeLinear(guard)

    with pytest.raises(PermanentAppError):
        await linear.get_issue("ENG-404")
