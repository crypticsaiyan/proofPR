"""Typed configuration loaded from the environment and an optional .env file.

Every secret is a :class:`~pydantic.SecretStr`, so accidental logging or
interpolation into a prompt yields ``**********`` rather than the credential.
Keys are documented in ``docs/CONFIGURATION.md`` and mirrored in ``.env.example``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from proofpr.domain.enums import Arm
from proofpr.observability.tracing import configure_tracing

Environment = Literal["dev", "eval", "prod"]


def _blank_to_none(value: object) -> object:
    """Treat an unset optional field the same whether it is absent or blank.

    ``KEY=`` in a `.env` file is how `.env.example` documents "not set" for
    every optional key, but pydantic reads it as the empty string rather than
    as absent, which fails validation on anything typed narrower than `str`.
    """
    return None if value == "" else value


class DiscordSettings(BaseSettings):
    """Discord gateway and channel configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DISCORD_", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    guild_id: int | None = None
    intake_channel_id: int | None = None
    eval_channel_id: int | None = None

    _blank_ids = field_validator("guild_id", "intake_channel_id", "eval_channel_id", mode="before")(
        _blank_to_none
    )


class GitHubSettings(BaseSettings):
    """GitHub credentials scoped to the single target repository."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="GITHUB_", extra="ignore")

    token: SecretStr = SecretStr("")
    owner: str = ""
    repo: str = ""
    webhook_secret: SecretStr = SecretStr("")


class LinearSettings(BaseSettings):
    """Linear API key and the single team issues are filed into."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LINEAR_", extra="ignore")

    api_key: SecretStr = SecretStr("")
    team_id: str = ""
    webhook_secret: SecretStr = SecretStr("")


class ModelSettings(BaseSettings):
    """OpenRouter routing.

    The cheap model handles intent classification, duplicate verdicts, and the
    injection classifier. The strong model is reserved for test synthesis and
    patches, which is what makes early pipeline stages worth their latency.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")
    cheap: str = Field(default="anthropic/claude-haiku-4.5", alias="MODEL_CHEAP")
    strong: str = Field(default="anthropic/claude-opus-5", alias="MODEL_STRONG")


class Caps(BaseSettings):
    """Hard limits enforced by the runner, not by prompt instructions."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    max_runs: int = Field(default=200, ge=1, alias="MAX_RUNS")
    max_spend_usd: float = Field(default=25.0, gt=0, alias="MAX_SPEND_USD")
    max_patch_attempts: int = Field(default=3, ge=1, le=10, alias="MAX_PATCH_ATTEMPTS")
    sandbox_timeout_seconds: int = Field(default=60, ge=5, le=600, alias="SANDBOX_TIMEOUT_SECONDS")


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PROOFPR_",
        extra="ignore",
    )

    env: Environment = "dev"
    log_level: str = "INFO"
    config_file: Path = Path("config/proofpr.toml")
    db_path: Path = Path("var/proofpr.db")
    arms: Arm = Arm.PROOFPR

    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    linear: LinearSettings = Field(default_factory=LinearSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    caps: Caps = Field(default_factory=Caps)


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from the current environment.

    Every entry point (CLI commands, the Discord bot, the eval harness) calls
    this once at start, which is why tracing is configured here rather than in
    each of them separately.

    Returns:
        Settings populated from environment variables and ``.env``.
    """
    configure_tracing()
    return Settings()
