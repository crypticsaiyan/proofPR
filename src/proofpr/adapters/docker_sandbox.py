"""Container sandbox for model-written code.

The flags here are the security control, not a performance tuning exercise. Each
one is load bearing and two of them are only discoverable by actually running the
thing: a read-only root leaves Python with no temporary directory, and pytest
insists on writing a cache unless told not to.

Nothing in this module ever receives a credential. The environment passed to the
container is constructed here, from scratch, and the host environment is not
consulted.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofpr.domain.errors import SandboxError
from proofpr.observability.logging import get_logger
from proofpr.observability.tracing import get_tracer

#: Output beyond this is truncated before it can reach a prompt or a payload.
MAX_OUTPUT_BYTES = 64 * 1024

logger = get_logger("proofpr.sandbox")


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource and isolation limits applied to every invocation."""

    image: str = "proofpr-sandbox:local"
    timeout_seconds: int = 60
    cpus: float = 1.0
    memory: str = "1g"
    pids_limit: int = 256
    tmpfs_size: str = "64m"


class DockerSandbox:
    """Runs commands against a worktree inside a locked-down container."""

    def __init__(self, limits: SandboxLimits | None = None, *, docker: str = "docker") -> None:
        """Build a sandbox runner.

        Args:
            limits: Resource and isolation limits.
            docker: Path to the docker executable.
        """
        self.limits = limits or SandboxLimits()
        self._docker = docker

    def build_argv(self, worktree: Path, command: list[str]) -> list[str]:
        """Return the exact ``docker run`` argument list for an invocation.

        Exposed so tests can assert on the flags directly. A sandbox whose
        isolation is only checked by running it is a sandbox whose isolation
        silently degrades the first time someone edits this method.
        """
        limits = self.limits
        return [
            self._docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            # S108: this is a tmpfs mount inside a throwaway container, not a
            # host temporary file.
            f"/tmp:rw,noexec,nosuid,size={limits.tmpfs_size}",  # noqa: S108
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(limits.cpus),
            "--memory",
            limits.memory,
            "--pids-limit",
            str(limits.pids_limit),
            # An empty environment, then the base image's own variables cleared
            # explicitly so nothing the project did not put there survives.
            "--env-file",
            "/dev/null",
            "-e",
            "GPG_KEY=",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            # A checkout is not an installed package. Both common layouts are made
            # importable: `src/` (a src-layout package, or flat modules kept there)
            # and the repository root.
            "-e",
            "PYTHONPATH=/work/src:/work",
            "-v",
            f"{worktree}:/work:ro",
            "-w",
            "/work",
            "--entrypoint",
            command[0],
            limits.image,
            *command[1:],
        ]

    async def run(
        self,
        *,
        worktree: str,
        command: list[str],
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Run a command in the sandbox and capture its output.

        Args:
            worktree: Host path mounted read-only at ``/work``.
            command: Argument list. The first element becomes the entrypoint.
            timeout_seconds: Overrides the configured wall timeout.

        Returns:
            ``exit_code``, ``stdout``, ``stderr``, ``duration_ms``, ``timed_out``,
            and the ``argv`` used, so a human can rerun it verbatim.

        Raises:
            SandboxError: Docker itself could not be started.
        """
        path = await asyncio.to_thread(lambda: Path(worktree).resolve())
        if not await asyncio.to_thread(path.is_dir):
            raise SandboxError(f"worktree does not exist: {path}")

        argv = self.build_argv(path, command)
        timeout = timeout_seconds or self.limits.timeout_seconds
        started = time.monotonic()

        with get_tracer().start_as_current_span("sandbox.run") as span:
            span.set_attribute("proofpr.sandbox_image", self.limits.image)
            span.set_attribute("proofpr.sandbox_timeout_seconds", timeout)
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise SandboxError(f"could not start docker: {error}") from error

            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                process.kill()
                stdout, stderr = await process.communicate()

            duration_ms = int((time.monotonic() - started) * 1000)
            span.set_attribute("proofpr.exit_code", process.returncode or 0)
            span.set_attribute("proofpr.timed_out", timed_out)
            span.set_attribute("proofpr.duration_ms", duration_ms)
            logger.info(
                "sandbox_run",
                command=shlex.join(command),
                exit_code=process.returncode,
                timed_out=timed_out,
                duration_ms=duration_ms,
            )
            return {
                "exit_code": process.returncode,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
                "duration_ms": duration_ms,
                "timed_out": timed_out,
                "argv": argv,
            }

    async def run_pytest(
        self,
        *,
        worktree: str,
        target: str = ".",
        extra_args: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Run pytest in the sandbox with the flags a read-only mount requires."""
        command = [
            "python",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            *(extra_args or []),
            target,
        ]
        return await self.run(worktree=worktree, command=command, timeout_seconds=timeout_seconds)

    async def available(self) -> bool:
        """Return whether the docker daemon can be reached at all."""
        try:
            process = await asyncio.create_subprocess_exec(
                self._docker,
                "version",
                "--format",
                "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return False
        await process.communicate()
        return process.returncode == 0


def prepare_worktree(path: Path) -> None:
    """Make a worktree readable by the container's unprivileged user.

    The sandbox runs as uid 10002, and a directory created by
    :func:`tempfile.mkdtemp` is mode 0700 owned by the host user. Mounting one
    without widening it produces a permission error from inside the container
    that reads like a pytest bug and is not one. Read and execute only: nothing
    here makes the mount writable, and it is mounted read-only regardless.
    """
    path.chmod(0o755)
    for child in path.rglob("*"):
        child.chmod(0o755 if child.is_dir() else 0o644)


def _truncate(raw: bytes, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Decode and truncate captured output.

    Truncation happens here rather than at the call site because every consumer
    of sandbox output either puts it in a prompt or in a payload, and both have
    size limits that a runaway test loop would otherwise blow through.
    """
    text = raw.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[truncated {len(text) - limit} characters]...\n{text[-half:]}"
