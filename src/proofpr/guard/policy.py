"""The operation allowlist.

Loaded from ``config/policy.yaml``. Anything not listed is refused. The file may
tighten at any time; widening it requires an ADR and a CODEOWNERS review, which
is a process rule this module deliberately cannot enforce and the reviewer must.

Constraints in the policy file are prose, aimed at a human reader. The machine
checks in :meth:`Policy.check` are the enforcement, and each one names the rule
it applied so refusals are countable rather than anecdotal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from proofpr.domain.errors import ConfigurationError, GuardBlockedError
from proofpr.domain.models import WriteIntent

#: A local copy overrides the packaged default, so a deployment can tighten the
#: allowlist without editing an installed package. The packaged file is the one
#: under CODEOWNERS review, and it is used whenever no local copy exists.
LOCAL_POLICY_PATH = Path("config/policy.yaml")
PACKAGED_POLICY_PATH = Path(__file__).parent.parent / "defaults" / "policy.yaml"

#: Applications the policy must describe. A missing section is a configuration
#: error rather than an empty allowlist, because an empty allowlist silently
#: refuses everything and looks like a bug in the pipeline.
REQUIRED_SECTIONS = frozenset({"discord", "github", "linear", "egress"})


@dataclass(frozen=True, slots=True)
class DiffRules:
    """Which paths a patch may touch."""

    may_touch: tuple[str, ...]
    may_add_one_file_under: tuple[str, ...]
    never_touch: tuple[str, ...]
    never_delete_existing_tests: bool


class Policy:
    """An immutable, loaded operation allowlist."""

    def __init__(self, document: dict[str, Any], *, source: Path | None = None) -> None:
        """Build a policy from an already-parsed document.

        Args:
            document: The parsed YAML.
            source: Where it came from, for error messages.

        Raises:
            ConfigurationError: A required section is missing or malformed.
        """
        self.source = source
        missing = REQUIRED_SECTIONS - document.keys()
        if missing:
            raise ConfigurationError(
                f"policy is missing required sections: {', '.join(sorted(missing))}"
            )

        self._allowed: dict[str, dict[str, str]] = {}
        self._denied: dict[str, frozenset[str]] = {}
        for app in sorted(REQUIRED_SECTIONS - {"egress"}):
            section = document[app] or {}
            self._allowed[app] = {
                entry["op"]: entry.get("constraint", "") for entry in section.get("allowed", [])
            }
            self._denied[app] = frozenset(section.get("denied_always", []))
            overlap = self._denied[app] & self._allowed[app].keys()
            if overlap:
                raise ConfigurationError(
                    f"{app}: operations both allowed and denied: {', '.join(sorted(overlap))}"
                )

        github_diff = (document["github"] or {}).get("diff_rules", {})
        self.diff_rules = DiffRules(
            may_touch=tuple(github_diff.get("may_touch", ())),
            may_add_one_file_under=tuple(github_diff.get("may_add_one_file_under", ())),
            never_touch=tuple(github_diff.get("never_touch", ())),
            never_delete_existing_tests=bool(github_diff.get("never_delete_existing_tests", True)),
        )
        self.egress = document["egress"] or {}
        self.branch_prefix_required = True

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        """Load a policy from disk.

        Args:
            path: Policy file. Defaults to ``config/policy.yaml``.

        Raises:
            ConfigurationError: The file is missing or is not a mapping.
        """
        resolved = path or (
            LOCAL_POLICY_PATH if LOCAL_POLICY_PATH.is_file() else PACKAGED_POLICY_PATH
        )
        if not resolved.is_file():
            raise ConfigurationError(f"policy file not found: {resolved}")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ConfigurationError(f"policy file is not a mapping: {resolved}")
        return cls(document, source=resolved)

    def allowed_operations(self, app: str) -> frozenset[str]:
        """Return the operations permitted for one application."""
        return frozenset(self._allowed.get(app, {}))

    def denied_operations(self, app: str) -> frozenset[str]:
        """Return the operations denied unconditionally for one application."""
        return self._denied.get(app, frozenset())

    def check(self, intent: WriteIntent, *, branch_prefix: str = "proofpr/") -> None:
        """Refuse the write unless the policy permits it.

        Args:
            intent: The write about to be attempted.
            branch_prefix: Required prefix for branches this agent creates.

        Raises:
            GuardBlockedError: The operation is denied, unknown, or violates a
                constraint the policy states.
        """
        app, operation = intent.app, intent.short_operation

        if operation in self._denied.get(app, frozenset()):
            raise GuardBlockedError(
                f"{intent.operation} is denied unconditionally",
                operation=intent.operation,
                rule="denied_always",
            )

        if operation not in self._allowed.get(app, {}):
            raise GuardBlockedError(
                f"{intent.operation} is not in the allowlist",
                operation=intent.operation,
                rule="not_allowlisted",
            )

        if app == "github" and operation == "create_branch":
            name = str(intent.payload.get("branch", ""))
            if not name.startswith(branch_prefix):
                raise GuardBlockedError(
                    f"branch {name!r} does not start with {branch_prefix!r}",
                    operation=intent.operation,
                    rule="branch_prefix",
                )

        if app == "github" and operation == "open_pr":
            if not intent.payload.get("draft", False):
                raise GuardBlockedError(
                    "pull requests are opened as drafts and promoted only after CI succeeds",
                    operation=intent.operation,
                    rule="draft_only",
                )
            if intent.payload.get("head") == intent.payload.get("base"):
                raise GuardBlockedError(
                    "head and base are the same ref",
                    operation=intent.operation,
                    rule="head_not_base",
                )

        if app == "github" and operation == "mark_ready" and not intent.payload.get("ci_success"):
            raise GuardBlockedError(
                "mark_ready requires a CI success read back from the checks API",
                operation=intent.operation,
                rule="ci_success_required",
            )

        if app == "discord":
            mentions = intent.payload.get("allowed_mentions")
            if mentions not in (None, "none"):
                raise GuardBlockedError(
                    f"allowed_mentions must be none, got {mentions!r}",
                    operation=intent.operation,
                    rule="no_mentions",
                )

    def check_diff(self, paths_changed: dict[str, str]) -> None:
        """Refuse a patch whose diff touches paths it may not.

        Args:
            paths_changed: Mapping of repository path to change kind, one of
                ``added``, ``modified``, or ``deleted``.

        Raises:
            GuardBlockedError: The diff leaves the permitted paths, adds more
                than one test file, or deletes an existing test.
        """
        added_tests: list[str] = []
        for path, kind in sorted(paths_changed.items()):
            if any(fnmatch(path, pattern) for pattern in self.diff_rules.never_touch):
                raise GuardBlockedError(
                    f"patch touches a forbidden path: {path}",
                    operation="github.push",
                    rule="never_touch",
                )
            under_tests = any(
                fnmatch(path, pattern) for pattern in self.diff_rules.may_add_one_file_under
            )
            if under_tests:
                if kind == "added":
                    added_tests.append(path)
                    continue
                if self.diff_rules.never_delete_existing_tests and kind in {"deleted", "modified"}:
                    raise GuardBlockedError(
                        f"patch {kind} an existing test: {path}",
                        operation="github.push",
                        rule="never_delete_existing_tests",
                    )
            elif not any(fnmatch(path, pattern) for pattern in self.diff_rules.may_touch):
                raise GuardBlockedError(
                    f"patch touches a path outside the allowed set: {path}",
                    operation="github.push",
                    rule="out_of_allowed_paths",
                )
            elif kind == "deleted":
                raise GuardBlockedError(
                    f"patch deletes a source file: {path}",
                    operation="github.push",
                    rule="no_source_deletion",
                )

        if len(added_tests) > 1:
            raise GuardBlockedError(
                f"patch adds {len(added_tests)} test files, at most one is allowed",
                operation="github.push",
                rule="one_test_file",
            )


def compile_secret_patterns(extra: list[str] | None = None) -> list[re.Pattern[str]]:
    """Compile the credential-shaped patterns used by the egress scanner.

    Args:
        extra: Additional literal prefixes from the policy file.

    Returns:
        Compiled patterns, ordered so the most specific match first.
    """
    literals = [re.escape(item) + r"[A-Za-z0-9_\-]{8,}" for item in (extra or [])]
    return [re.compile(pattern) for pattern in literals]
