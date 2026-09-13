"""Composition root.

The one place adapters are constructed from settings. Steps receive ports and
never reach for configuration themselves, which is what keeps them testable
against fakes and keeps credentials out of the pipeline's vocabulary.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from proofpr.adapters import (
    DiscordAdapter,
    DockerSandbox,
    FaultInjector,
    GitHubAdapter,
    LinearAdapter,
    SandboxLimits,
)
from proofpr.adapters.memory import FakeDiscord, FakeGitHub, FakeLinear, FakeSandbox
from proofpr.adapters.openrouter import OpenRouterAdapter
from proofpr.domain.errors import ConfigurationError
from proofpr.domain.run import Report
from proofpr.guard import Guard
from proofpr.ledger import Ledger
from proofpr.settings import Settings
from proofpr.steps.approve import AutoApprover, DenyingApprover

#: ``https://discord.com/channels/<guild>/<channel>/<message>``
DISCORD_LINK = re.compile(
    r"https?://(?:\w+\.)?discord\.com/channels/(?P<guild>\d+|@me)/"
    r"(?P<channel>\d+)/(?P<message>\d+)"
)

#: As with the policy, a local copy wins and the packaged default is the
#: fallback, so the tool works wherever it is started from.
LOCAL_CONFIG = Path("config/proofpr.toml")
PACKAGED_CONFIG = Path(__file__).parent / "defaults" / "proofpr.toml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the non-secret configuration file.

    Prefers `config/proofpr.toml` in the working directory, then the packaged
    default. A missing explicit path is an error; a missing local copy is not.
    """
    resolved = path or (LOCAL_CONFIG if LOCAL_CONFIG.is_file() else PACKAGED_CONFIG)
    if not resolved.is_file():
        raise ConfigurationError(f"no configuration file at {resolved}")
    with resolved.open("rb") as handle:
        return tomllib.load(handle)


@dataclass
class Apps:
    """Every adapter a run needs, already guarded.

    The types are the concrete adapters rather than the protocols because this is
    the composition root, and the in-memory bundle deliberately substitutes
    objects that satisfy the same protocols.
    """

    discord: Any
    github: Any
    linear: Any
    sandbox: Any
    guard: Guard
    config: dict[str, Any]

    async def aclose(self) -> None:
        """Close every underlying HTTP client, if there is one."""
        for adapter in (self.discord, self.github, self.linear):
            if hasattr(adapter, "aclose"):
                await adapter.aclose()


def build_apps(
    settings: Settings,
    *,
    config_path: Path | None = None,
    policy_path: Path | None = None,
    faults: FaultInjector | None = None,
) -> Apps:
    """Construct every adapter from settings and configuration.

    Missing credentials are not an error here. They surface as failing health
    checks in `doctor`, which reports on every application in one pass instead
    of stopping at the first unset variable.
    """
    config = load_config(config_path)
    repo = config.get("repo", {})
    sandbox_config = config.get("sandbox", {})

    guard = Guard.load(
        policy_path=policy_path,
        canary=config.get("security", {}).get("canary"),
        branch_prefix=str(repo.get("branch_prefix", "proofpr/")),
    )

    return Apps(
        discord=DiscordAdapter(
            bot_token=settings.discord.bot_token.get_secret_value(),
            guard=guard,
            faults=faults,
            health_channel_id=settings.discord.eval_channel_id
            or settings.discord.intake_channel_id,
        ),
        github=GitHubAdapter(
            token=settings.github.token.get_secret_value(),
            owner=settings.github.owner or str(repo.get("owner", "")),
            repo=settings.github.repo or str(repo.get("name", "")),
            guard=guard,
            faults=faults,
            default_branch=str(repo.get("default_branch", "main")),
        ),
        linear=LinearAdapter(
            api_key=settings.linear.api_key.get_secret_value(),
            team_id=settings.linear.team_id,
            guard=guard,
            faults=faults,
        ),
        sandbox=DockerSandbox(
            SandboxLimits(
                image=str(sandbox_config.get("image", "proofpr-sandbox:local")),
                timeout_seconds=int(
                    sandbox_config.get("timeout_seconds", settings.caps.sandbox_timeout_seconds)
                ),
                cpus=float(sandbox_config.get("cpus", 1.0)),
                memory=str(sandbox_config.get("memory", "1g")),
                pids_limit=int(sandbox_config.get("pids_limit", 256)),
            )
        ),
        guard=guard,
        config=config,
    )


def build_memory_apps(
    *,
    config_path: Path | None = None,
    policy_path: Path | None = None,
) -> Apps:
    """Build an Apps bundle backed by in-memory applications.

    Used by `proofpr run --dry-run` and by the evaluation smoke job. The guard,
    the pipeline, the ledger, and every rule are the real ones; only the four
    applications are replaced, so a dry run exercises the same guarded write path
    as a real one.
    """
    config = load_config(config_path)
    guard = Guard.load(
        policy_path=policy_path,
        branch_prefix=str(config.get("repo", {}).get("branch_prefix", "proofpr/")),
    )
    return Apps(
        discord=FakeDiscord(guard),
        github=FakeGitHub(guard),
        linear=FakeLinear(guard),
        sandbox=FakeSandbox(),
        guard=guard,
        config=config,
    )


def build_approver(settings: Settings, config: dict[str, Any]) -> Any:  # noqa: ANN401
    """Choose an approver from configuration.

    Three configurations, in order of trust. Evaluation approves automatically so
    numbers measure the agent rather than human availability. A deployment that
    has set `approval.required = false` has decided the proof is enough. Anything
    else refuses, because an agent with nobody to ask must not publish.
    """
    approval = config.get("approval", {})
    if settings.env == "eval" or approval.get("auto_approve") is True:
        return AutoApprover()
    if approval.get("required") is False:
        return AutoApprover()
    return DenyingApprover()


def build_model(settings: Settings, *, guard: Guard | None = None) -> OpenRouterAdapter:
    """Construct the model client.

    Kept separate from :func:`build_apps` because a run without a model is a
    supported configuration: the rules still triage, and the pipeline says so
    rather than pretending a classification happened.
    """
    return OpenRouterAdapter(
        api_key=settings.models.openrouter_api_key.get_secret_value(),
        cheap_model=settings.models.cheap,
        strong_model=settings.models.strong,
        guard=guard or Guard.load(),
    )


def load_report(source: str) -> Report:
    """Build a report from a fixture path or a Discord message link.

    Raises:
        ConfigurationError: The source is neither a readable fixture nor a
            recognisable Discord message link.
    """
    path = Path(source)
    if path.is_file():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Report(
            source=str(document.get("source", "fixture")),
            text=str(document["text"]),
            author=str(document.get("author", "")),
            url=str(path),
        )

    match = DISCORD_LINK.fullmatch(source.strip())
    if match is None:
        raise ConfigurationError(f"{source!r} is neither a fixture file nor a Discord message link")
    return Report(
        source="discord",
        text="",
        channel_id=int(match.group("channel")),
        message_id=int(match.group("message")),
        url=source.strip(),
    )


def open_ledger(settings: Settings) -> Ledger:
    """Open the run ledger at the configured path, applying migrations."""
    return Ledger(settings.db_path)
