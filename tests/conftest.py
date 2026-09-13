"""Shared pytest fixtures.

No test in ``tests/unit``, ``tests/property``, or ``tests/contract`` may touch
the network or the filesystem outside ``tmp_path``. Tests that hit real
applications carry the ``live`` marker and are excluded by default.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from proofpr.guard import EgressScanner, Guard
from proofpr.guard.policy import Policy

CREDENTIAL_PREFIXES = (
    "PROOFPR_",
    "DISCORD_",
    "GITHUB_",
    "LINEAR_",
    "OPENROUTER_",
    "MODEL_",
    "MAX_",
    "SANDBOX_",
    "OTEL_",
)

#: Repository root, resolved once. Tests chdir into tmp_path, so anything that
#: reads a committed file must go through this.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The evaluation harness is a script directory rather than a package, kept out
#: of the wheel so its hidden tests cannot be imported from installed code. Tests
#: that exercise it need it on the path, the same way `proofpr eval` puts it
#: there at runtime.
EVAL_DIR = REPO_ROOT / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run every test with no credentials and no discoverable ``.env``.

    Without this, a developer's real environment would leak into unit tests and
    a passing suite would prove nothing about a clean machine. Changing the
    working directory also stops pydantic-settings from finding the repository's
    own ``.env``.
    """
    for key in [k for k in os.environ if k.startswith(CREDENTIAL_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def policy() -> Policy:
    """Load the real ``config/policy.yaml``.

    Deliberately the shipped file rather than a fixture: a test that passes
    against an invented policy says nothing about the policy that actually
    governs the agent.
    """
    return Policy.load(REPO_ROOT / "src" / "proofpr" / "defaults" / "policy.yaml")


@pytest.fixture
def guard(policy: Policy) -> Guard:
    """A guard over the real policy, with a known canary and no environment secrets."""
    scanner = EgressScanner.from_policy(policy.egress, canary="canary-9f83b1e4c7a2d5", environ={})
    return Guard(policy, scanner)


@pytest.fixture
def intent_factory() -> object:
    """Return a helper that builds write intents with sensible defaults."""
    from proofpr.domain.models import WriteIntent

    def make(operation: str, **payload: object) -> WriteIntent:
        app = operation.split(".", 1)[0]
        return WriteIntent(
            run_id="r-test",
            app=app,
            operation=operation,
            target=str(payload.get("target", "target")),
            payload=dict(payload),
        )

    return make
