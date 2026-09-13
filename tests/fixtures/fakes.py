"""Re-exports of the in-memory applications.

The fakes live in the package itself, because `proofpr run --dry-run` and the
evaluation smoke job need them too. Tests import them from here so the import
path in a test reads like a test.
"""

from proofpr.adapters.memory import (
    FakeApp,
    FakeDiscord,
    FakeGitHub,
    FakeLinear,
    FakeSandbox,
)

__all__ = [
    "FakeApp",
    "FakeDiscord",
    "FakeGitHub",
    "FakeLinear",
    "FakeSandbox",
]
