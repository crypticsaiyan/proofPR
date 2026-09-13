"""The sandbox flags are the security control, so they are asserted directly."""

from __future__ import annotations

from pathlib import Path

import pytest

from proofpr.adapters.docker_sandbox import (
    MAX_OUTPUT_BYTES,
    DockerSandbox,
    SandboxLimits,
    _truncate,
)
from proofpr.domain.errors import SandboxError


@pytest.fixture
def sandbox() -> DockerSandbox:
    """A sandbox with the shipped defaults."""
    return DockerSandbox(SandboxLimits(image="proofpr-sandbox:test"))


def argv_pairs(argv: list[str]) -> dict[str, str]:
    """Return flag-to-value pairs from a docker argument list."""
    return {argv[i]: argv[i + 1] for i in range(len(argv) - 1)}


def test_the_container_has_no_network(sandbox: DockerSandbox, tmp_path: Path) -> None:
    argv = sandbox.build_argv(tmp_path, ["python", "-c", "pass"])

    assert argv_pairs(argv)["--network"] == "none"


def test_the_container_is_read_only_with_a_writable_tmpfs(
    sandbox: DockerSandbox, tmp_path: Path
) -> None:
    # Both are required together: a read-only root alone leaves Python with no
    # temporary directory and every invocation fails before it starts.
    argv = sandbox.build_argv(tmp_path, ["python", "-c", "pass"])

    assert "--read-only" in argv
    assert argv_pairs(argv)["--tmpfs"].startswith("/tmp:rw,noexec,nosuid")


def test_the_container_has_no_capabilities_and_cannot_gain_any(
    sandbox: DockerSandbox, tmp_path: Path
) -> None:
    argv = sandbox.build_argv(tmp_path, ["python", "-c", "pass"])
    pairs = argv_pairs(argv)

    assert pairs["--cap-drop"] == "ALL"
    assert pairs["--security-opt"] == "no-new-privileges"


def test_resources_are_bounded(sandbox: DockerSandbox, tmp_path: Path) -> None:
    pairs = argv_pairs(sandbox.build_argv(tmp_path, ["python", "-c", "pass"]))

    assert pairs["--cpus"] == "1.0"
    assert pairs["--memory"] == "1g"
    assert pairs["--pids-limit"] == "256"


def test_the_environment_is_empty_and_the_base_image_variables_are_cleared(
    sandbox: DockerSandbox, tmp_path: Path
) -> None:
    argv = sandbox.build_argv(tmp_path, ["python", "-c", "pass"])

    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == "/dev/null"
    assert "GPG_KEY=" in argv
    assert not any(value.startswith(("GITHUB_", "LINEAR_", "OPENROUTER_")) for value in argv)


def test_the_worktree_is_mounted_read_only(sandbox: DockerSandbox, tmp_path: Path) -> None:
    argv = sandbox.build_argv(tmp_path, ["python", "-c", "pass"])

    assert argv_pairs(argv)["-v"] == f"{tmp_path}:/work:ro"


def test_pytest_runs_without_writing_a_cache(sandbox: DockerSandbox) -> None:
    # pytest writes a cache by default, which fails on a read-only mount. The
    # flag is part of the contract, not a preference.
    import asyncio

    recorded: list[list[str]] = []

    async def fake_run(*, command: list[str], **kwargs: object) -> dict[str, object]:
        recorded.append(command)
        return {"exit_code": 0}

    sandbox.run = fake_run  # type: ignore[method-assign]
    asyncio.run(sandbox.run_pytest(worktree="/tmp/x"))

    assert "no:cacheprovider" in recorded[0]


async def test_a_missing_worktree_is_an_error_not_a_silent_pass(
    sandbox: DockerSandbox, tmp_path: Path
) -> None:
    with pytest.raises(SandboxError, match="worktree does not exist"):
        await sandbox.run(worktree=str(tmp_path / "nope"), command=["python", "-c", "pass"])


def test_output_is_truncated_in_the_middle_so_both_ends_survive() -> None:
    raw = (b"a" * MAX_OUTPUT_BYTES) + b"TAIL"

    truncated = _truncate(raw)

    assert truncated.startswith("a")
    assert truncated.endswith("TAIL")
    assert "truncated" in truncated


def test_short_output_is_returned_untouched() -> None:
    assert _truncate(b"1 passed") == "1 passed"
