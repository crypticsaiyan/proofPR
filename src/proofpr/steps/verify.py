"""The three-way consistency assertion.

Section 4.7 of AGENTS.md. Each individual write is read back at the moment it
happens. This is the separate question of whether Discord, GitHub, and Linear agree
with each other once the run is over, which is where drift actually shows up: a
pull request opened but never linked, an issue left in triage after a fix
shipped, a reply that never reached the reporter.

Every expectation is stated per outcome, read from the applications, and recorded
as a boolean per application. A run that diverges is reported rather than
silently repaired; the reconciler in M7 does the repairing, and it can only do
that honestly if divergence was recorded when it happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proofpr.domain.enums import Outcome
from proofpr.observability.logging import get_logger

logger = get_logger("proofpr.verify")


@dataclass
class Consistency:
    """What each application says, and whether it matches the intent."""

    outcome: Outcome
    checks: dict[str, bool] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """Whether every application agrees with the run's terminal state."""
        return bool(self.checks) and all(self.checks.values())

    def record(self, app: str, ok: bool, problem: str = "") -> None:
        """Record one application's verdict."""
        self.checks[app] = ok
        if not ok and problem:
            self.problems.append(f"{app}: {problem}")


async def assert_consistent(
    *,
    outcome: Outcome,
    run_id: str,
    github: Any,  # noqa: ANN401 - a GitHubPort
    linear: Any,  # noqa: ANN401
    discord: Any,  # noqa: ANN401
    pr_number: str | None,
    linear_issue_id: str | None,
    discord_message_id: str | None,
    discord_channel_id: int | None,
    expect_ready: bool,
) -> Consistency:
    """Read Discord, GitHub, and Linear and check they agree with the run's outcome.

    Reads only. This step never repairs anything, because a repair that happens
    during verification cannot be distinguished from a system that was correct in
    the first place, and the difference is the whole point of measuring.
    """
    marker = f"proofpr-run:{run_id}"
    result = Consistency(outcome=outcome)

    # GitHub: a pull request exists, carries the marker, and is in the expected
    # draft state. For every other outcome, no pull request should exist at all.
    if outcome is Outcome.PR_OPENED:
        try:
            pull = await github.get_pr(int(pr_number or 0))
            ok = marker in (pull.get("body") or "") and bool(pull.get("draft")) is not expect_ready
            result.record(
                "github",
                ok,
                "" if ok else f"pull request {pr_number} does not match the intended state",
            )
        except Exception as error:  # noqa: BLE001 - a missing pull request is a divergence
            result.record("github", False, f"pull request {pr_number} could not be read: {error}")
    else:
        found = await github.search_issues(marker)
        result.record(
            "github",
            not found,
            "" if not found else f"{len(found)} pull requests or issues carry this run's marker",
        )

    # Linear: an issue of record exists for every outcome except not_a_bug.
    if outcome is Outcome.NOT_A_BUG:
        result.record("linear", linear_issue_id is None, "an issue was filed for a non-bug")
    elif linear_issue_id is None:
        result.record("linear", False, "no issue of record was created")
    else:
        try:
            issue = await linear.get_issue(linear_issue_id)
            body = f"{issue.get('description') or ''}"
            comments = " ".join(
                str(node.get("body", "")) for node in issue.get("comments", {}).get("nodes", [])
            )
            ok = marker in body or marker in comments
            result.record("linear", ok, "" if ok else "the issue does not carry the run marker")
        except Exception as error:  # noqa: BLE001 - an unreadable issue is a divergence
            result.record("linear", False, f"issue {linear_issue_id} could not be read: {error}")

    # Discord: the reporter was answered, in their own channel. A run started
    # from a fixture or the CLI has no reporter, and holding it to a reply it
    # could never make would mark every such run divergent.
    if discord_channel_id is None:
        result.record("discord", True)
    elif discord_message_id is None:
        result.record("discord", False, "the reporter was never answered")
    else:
        try:
            message = await discord.get_message(discord_channel_id, int(discord_message_id))
            ok = marker in str(message.get("content", ""))
            result.record("discord", ok, "" if ok else "the reply does not carry the run marker")
        except Exception as error:  # noqa: BLE001 - an unreadable reply is a divergence
            result.record("discord", False, f"the reply could not be read: {error}")

    logger.info(
        "four_way_verified",
        run_id=run_id,
        consistent=result.consistent,
        problems=result.problems,
    )
    return result
