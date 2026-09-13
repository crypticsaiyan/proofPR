"""Environment verification.

`doctor` is the only thing in this project that is allowed to claim the
environment works, and it earns that by doing one real write and reading it back
for every application. A read-only check would pass with a token that cannot
push, which is exactly the failure worth catching before a run starts.

It never raises. Every application is checked in one pass and reported together,
because discovering four broken credentials one run at a time is four times the
work.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from proofpr.adapters.docker_sandbox import prepare_worktree
from proofpr.composition import Apps
from proofpr.domain.models import HealthCheck
from proofpr.ledger import Ledger

SMOKE_TEST = """
def test_sandbox_can_run_a_test():
    assert 1 + 1 == 2
"""


async def check_apps(apps: Apps) -> list[HealthCheck]:
    """Run every application health check concurrently."""
    results = await asyncio.gather(
        apps.discord.health(),
        apps.github.health(),
        apps.linear.health(),
        return_exceptions=True,
    )
    checks: list[HealthCheck] = []
    for adapter, result in zip((apps.discord, apps.github, apps.linear), results, strict=True):
        if isinstance(result, BaseException):
            checks.append(
                HealthCheck(app=adapter.app, check="health", ok=False, detail=str(result))
            )
        else:
            checks.extend(result)
    return checks


async def check_sandbox(apps: Apps) -> list[HealthCheck]:
    """Prove the sandbox image exists and can run a test under full isolation."""
    if not await apps.sandbox.available():
        return [
            HealthCheck(
                app="sandbox",
                check="docker daemon",
                ok=False,
                detail="docker is not reachable; start it or set a different runner",
            )
        ]

    with tempfile.TemporaryDirectory(prefix="proofpr-doctor-") as directory:
        (Path(directory) / "test_smoke.py").write_text(SMOKE_TEST, encoding="utf-8")
        prepare_worktree(Path(directory))
        started = time.monotonic()
        result = await apps.sandbox.run_pytest(worktree=directory, timeout_seconds=60)

    ok = result["exit_code"] == 0
    detail = f"image {apps.sandbox.limits.image}"
    if not ok:
        detail = (
            f"{detail}: {result['stderr'][-300:] or result['stdout'][-300:]}\n"
            "build it with `just sandbox-image`"
        )
    return [
        HealthCheck(
            app="sandbox",
            check="run a test with no network and a read-only root",
            ok=ok,
            detail=detail,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    ]


def check_ledger(ledger: Ledger) -> list[HealthCheck]:
    """Prove the ledger is writable and its chain verifies."""
    started = time.monotonic()
    try:
        run_id = ledger.start_run(source="doctor")
        ledger.append(run_id, "doctor_check", {"ok": True})
        intact, _ = ledger.verify_chain(run_id)
    except Exception as error:  # noqa: BLE001 - doctor reports, never raises
        return [HealthCheck(app="ledger", check="write and verify", ok=False, detail=str(error))]
    return [
        HealthCheck(
            app="ledger",
            check="write and verify the hash chain",
            ok=intact,
            detail=str(ledger.path),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    ]


async def run_doctor(apps: Apps, ledger: Ledger) -> list[HealthCheck]:
    """Run every check and return the results in reporting order."""
    checks = check_ledger(ledger)
    checks.extend(await check_sandbox(apps))
    checks.extend(await check_apps(apps))
    return checks
