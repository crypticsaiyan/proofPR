"""The allowlist refuses everything it does not name, and every constraint bites."""

from __future__ import annotations

import pytest

from proofpr.domain.errors import ConfigurationError, GuardBlockedError
from proofpr.domain.models import WriteIntent
from proofpr.guard.policy import Policy


def intent(operation: str, **payload: object) -> WriteIntent:
    """Build a write intent for one operation."""
    return WriteIntent(
        run_id="r-test",
        app=operation.split(".", 1)[0],
        operation=operation,
        target="target",
        payload=dict(payload),
    )


def test_shipped_policy_covers_every_application(policy: Policy) -> None:
    for app in ("discord", "github", "linear"):
        assert policy.allowed_operations(app), f"{app} has an empty allowlist"


def test_unlisted_operations_are_refused(policy: Policy) -> None:
    with pytest.raises(GuardBlockedError) as excinfo:
        policy.check(intent("github.transfer_repository"))

    assert excinfo.value.rule == "not_allowlisted"


@pytest.mark.parametrize(
    "operation",
    [
        "github.merge",
        "github.add_collaborator",
        "github.edit_workflow",
        "github.push_to_default_branch",
        "github.force_push",
        "linear.delete_issue",
        "discord.mention_everyone",
    ],
)
def test_dangerous_operations_are_denied_unconditionally(policy: Policy, operation: str) -> None:
    with pytest.raises(GuardBlockedError) as excinfo:
        policy.check(intent(operation))

    assert excinfo.value.rule == "denied_always"


def test_branches_must_carry_the_prefix(policy: Policy) -> None:
    policy.check(intent("github.create_branch", branch="proofpr/r-test"))

    with pytest.raises(GuardBlockedError, match="does not start with"):
        policy.check(intent("github.create_branch", branch="main"))


def test_pull_requests_open_as_drafts_only(policy: Policy) -> None:
    policy.check(intent("github.open_pr", draft=True, head="proofpr/x", base="main"))

    with pytest.raises(GuardBlockedError) as excinfo:
        policy.check(intent("github.open_pr", draft=False, head="proofpr/x", base="main"))
    assert excinfo.value.rule == "draft_only"


def test_a_pull_request_cannot_target_its_own_head(policy: Policy) -> None:
    with pytest.raises(GuardBlockedError, match="same ref"):
        policy.check(intent("github.open_pr", draft=True, head="main", base="main"))


def test_mark_ready_requires_a_ci_success_that_was_read_back(policy: Policy) -> None:
    with pytest.raises(GuardBlockedError) as excinfo:
        policy.check(intent("github.mark_ready", number=7))
    assert excinfo.value.rule == "ci_success_required"

    policy.check(intent("github.mark_ready", number=7, ci_success=True))


def test_discord_messages_may_never_mention(policy: Policy) -> None:
    policy.check(intent("discord.reply", content="hello"))
    policy.check(intent("discord.reply", content="hello", allowed_mentions="none"))

    with pytest.raises(GuardBlockedError) as excinfo:
        policy.check(intent("discord.reply", content="hi", allowed_mentions="everyone"))
    assert excinfo.value.rule == "no_mentions"


def test_a_malformed_policy_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="missing required sections"):
        Policy({"discord": {}})

    with pytest.raises(ConfigurationError, match="both allowed and denied"):
        Policy(
            {
                "discord": {"allowed": [{"op": "reply"}], "denied_always": ["reply"]},
                "github": {},
                "linear": {},
                "egress": {},
            }
        )


class TestDiffRules:
    """A patch may touch source and add one test, and nothing else."""

    def test_the_expected_shape_of_a_patch_is_allowed(self, policy: Policy) -> None:
        policy.check_diff(
            {
                "src/validkit/parse.py": "modified",
                "tests/test_parse_date_month.py": "added",
            }
        )

    @pytest.mark.parametrize(
        "path",
        [".github/workflows/ci.yml", "pyproject.toml", "conftest.py", "uv.lock"],
    )
    def test_forbidden_paths_are_refused(self, policy: Policy, path: str) -> None:
        with pytest.raises(GuardBlockedError) as excinfo:
            policy.check_diff({path: "modified"})
        assert excinfo.value.rule == "never_touch"

    def test_existing_tests_are_never_edited_or_deleted(self, policy: Policy) -> None:
        with pytest.raises(GuardBlockedError) as excinfo:
            policy.check_diff({"tests/test_existing.py": "modified"})
        assert excinfo.value.rule == "never_delete_existing_tests"

    def test_only_one_test_file_may_be_added(self, policy: Policy) -> None:
        with pytest.raises(GuardBlockedError, match="at most one"):
            policy.check_diff({"tests/a.py": "added", "tests/b.py": "added"})

    def test_source_outside_the_allowed_tree_is_refused(self, policy: Policy) -> None:
        with pytest.raises(GuardBlockedError) as excinfo:
            policy.check_diff({"scripts/deploy.sh": "added"})
        assert excinfo.value.rule == "out_of_allowed_paths"

    def test_source_files_are_never_deleted(self, policy: Policy) -> None:
        with pytest.raises(GuardBlockedError) as excinfo:
            policy.check_diff({"src/validkit/parse.py": "deleted"})
        assert excinfo.value.rule == "no_source_deletion"
