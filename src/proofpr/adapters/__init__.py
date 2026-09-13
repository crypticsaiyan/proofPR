"""Concrete clients for Discord, GitHub, Linear, and the Docker sandbox.

Each adapter owns its transport concerns only: timeouts, retries, rate limits,
and mapping failures onto the domain error hierarchy. No adapter decides what to
write; the pipeline does, and every write passes the guard first.
"""

from proofpr.adapters.base import FaultInjector, HttpAdapter
from proofpr.adapters.discord import DiscordAdapter
from proofpr.adapters.docker_sandbox import DockerSandbox, SandboxLimits
from proofpr.adapters.github import GitHubAdapter
from proofpr.adapters.linear import LinearAdapter

#: Every adapter that talks to an external application, for doctor and for the
#: contract tests that assert code and policy agree.
APP_ADAPTERS: tuple[type[HttpAdapter], ...] = (
    DiscordAdapter,
    GitHubAdapter,
    LinearAdapter,
)

__all__ = [
    "APP_ADAPTERS",
    "DiscordAdapter",
    "DockerSandbox",
    "FaultInjector",
    "GitHubAdapter",
    "HttpAdapter",
    "LinearAdapter",
    "SandboxLimits",
]
