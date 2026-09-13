"""The composition root wires real settings to real adapters, without credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from proofpr.composition import build_apps, load_config
from proofpr.domain.errors import ConfigurationError
from proofpr.settings import Settings
from tests.conftest import REPO_ROOT


@pytest.fixture
def settings() -> Settings:
    """Default settings, with no credentials present."""
    return Settings()


def test_the_shipped_example_configuration_parses_and_is_complete() -> None:
    config = load_config(REPO_ROOT / "src" / "proofpr" / "defaults" / "proofpr.toml")

    # Every section the composition root reads must exist in the example, or a
    # fresh clone silently falls back to defaults it never chose.
    for section in ("repo", "fix_class", "triage", "proof", "sandbox", "ci", "reconciler"):
        assert section in config, f"config/proofpr.example.toml is missing [{section}]"


def test_a_missing_configuration_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="no configuration file"):
        load_config(tmp_path / "nothing.toml")


def test_every_adapter_is_built_and_shares_one_guard(settings: Settings) -> None:
    apps = build_apps(
        settings,
        config_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "proofpr.toml",
        policy_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "policy.yaml",
    )

    assert {apps.discord.app, apps.github.app, apps.linear.app} == {
        "discord",
        "github",
        "linear",
    }
    for adapter in (apps.discord, apps.github, apps.linear):
        assert adapter._guard is apps.guard


def test_missing_credentials_do_not_stop_construction(settings: Settings) -> None:
    # doctor must be able to report on every application at once, which is only
    # possible if building them never raises on an unset variable.
    apps = build_apps(
        settings,
        config_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "proofpr.toml",
        policy_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "policy.yaml",
    )

    assert apps.github.slug == "OWNER/validkit"


def test_sandbox_limits_come_from_configuration(settings: Settings) -> None:
    apps = build_apps(
        settings,
        config_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "proofpr.toml",
        policy_path=REPO_ROOT / "src" / "proofpr" / "defaults" / "policy.yaml",
    )

    assert apps.sandbox.limits.pids_limit == 256
    assert apps.sandbox.limits.memory == "1g"
