"""Settings load from the environment and never expose secrets by accident."""

from __future__ import annotations

import pytest

from proofpr.domain.enums import Arm
from proofpr.settings import Settings, load_settings


def test_defaults_are_usable_without_any_environment() -> None:
    settings = load_settings()

    assert settings.env == "dev"
    assert settings.arms is Arm.PROOFPR
    assert settings.caps.max_patch_attempts == 3
    assert settings.caps.sandbox_timeout_seconds == 60


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROOFPR_ENV", "eval")
    monkeypatch.setenv("PROOFPR_ARMS", "no_gate")
    monkeypatch.setenv("MAX_SPEND_USD", "5")
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GITHUB_REPO", "validkit")

    settings = load_settings()

    assert settings.env == "eval"
    assert settings.arms is Arm.NO_GATE
    assert settings.caps.max_spend_usd == 5.0
    assert settings.github.owner == "acme"


def test_secrets_do_not_render_in_repr_or_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-supersecretvalue")

    settings = load_settings()

    rendered = f"{settings!r} {settings!s} {settings.model_dump()}"
    assert "supersecretvalue" not in rendered
    assert settings.github.token.get_secret_value() == "ghp_supersecretvalue"


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_caps_reject_nonsense_values(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("MAX_SPEND_USD", bad)

    with pytest.raises(ValueError, match="MAX_SPEND_USD"):
        Settings()
