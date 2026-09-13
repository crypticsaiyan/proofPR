"""Doctor reports on every application in one pass and never raises."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from proofpr.doctor import check_apps, check_ledger, check_sandbox
from proofpr.domain.models import HealthCheck
from proofpr.ledger import Ledger


class StubAdapter:
    """An adapter whose health result is scripted."""

    def __init__(self, app: str, result: list[HealthCheck] | Exception) -> None:
        """Build the stub."""
        self.app = app
        self._result = result

    async def health(self) -> list[HealthCheck]:
        """Return the scripted result, or raise the scripted error."""
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class StubSandbox:
    """A sandbox whose availability and result are scripted."""

    def __init__(self, *, available: bool = True, exit_code: int = 0) -> None:
        """Build the stub."""
        self._available = available
        self._exit_code = exit_code
        self.limits = type("Limits", (), {"image": "proofpr-sandbox:test"})()

    async def available(self) -> bool:
        """Report whether docker is reachable."""
        return self._available

    async def run_pytest(self, **kwargs: Any) -> dict[str, Any]:
        """Return a scripted pytest result."""
        return {"exit_code": self._exit_code, "stdout": "1 passed", "stderr": "boom"}


def apps_with(*adapters: StubAdapter, sandbox: StubSandbox | None = None) -> Any:
    """Build a stand-in Apps object holding only what doctor touches."""
    discord, github, linear = adapters
    return type(
        "StubApps",
        (),
        {
            "discord": discord,
            "github": github,
            "linear": linear,
            "sandbox": sandbox or StubSandbox(),
        },
    )()


ok = [HealthCheck(app="x", check="fine", ok=True)]


async def test_one_broken_application_does_not_hide_the_others() -> None:
    apps = apps_with(
        StubAdapter("discord", ok),
        StubAdapter("github", RuntimeError("bad credentials")),
        StubAdapter("linear", ok),
    )

    checks = await check_apps(apps)

    assert len(checks) == 3
    assert [check.ok for check in checks] == [True, False, True]
    assert "bad credentials" in checks[1].detail
    assert checks[1].app == "github"


async def test_a_missing_docker_daemon_is_reported_not_raised() -> None:
    checks = await check_sandbox(
        apps_with(*[StubAdapter(n, ok) for n in "abc"], sandbox=StubSandbox(available=False))
    )

    assert checks[0].ok is False
    assert "docker is not reachable" in checks[0].detail


async def test_a_failing_sandbox_suggests_building_the_image() -> None:
    checks = await check_sandbox(
        apps_with(*[StubAdapter(n, ok) for n in "abc"], sandbox=StubSandbox(exit_code=1))
    )

    assert checks[0].ok is False
    assert "just sandbox-image" in checks[0].detail


def test_the_ledger_check_writes_and_verifies(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")

    checks = check_ledger(ledger)

    assert checks[0].ok is True
    assert ledger.unfinished_runs()[0]["source"] == "doctor"


def test_an_unwritable_ledger_is_reported_not_raised(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.db")
    ledger.close()

    checks = check_ledger(ledger)

    assert checks[0].ok is False


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_the_sandbox_check_reports_the_image_it_used(exit_code: int) -> None:
    checks = await check_sandbox(
        apps_with(*[StubAdapter(n, ok) for n in "abc"], sandbox=StubSandbox(exit_code=exit_code))
    )

    assert "proofpr-sandbox:test" in checks[0].detail
