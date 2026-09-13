"""Stage 1 of reproduction: run the reported input and see what actually happens.

Section 4.4 of AGENTS.md. Before a model is asked to write a test, the report is
executed verbatim in the sandbox. This settles, with evidence rather than
inference, three questions a model would otherwise be asked to guess at:

- Does it crash at all?
- With which exception, from which frame?
- Is the failure in the project, or in the reporter's environment?

Everything downstream is built on the captured traceback, never on the report's
prose. A reporter who says "TypeError" and gets a ValueError has given us a
ValueError bug, and stage 1 is what notices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from proofpr.domain.enums import Reason
from proofpr.steps import worktree as wt
from proofpr.triage import repro_input
from proofpr.triage.fingerprint import EXCEPTION_PATTERN, normalise_path

#: Where the generated script lives inside the worktree.
SCRIPT_PATH = "proofpr_repro.py"

#: Failures that mean the environment is wrong, not the code.
ENVIRONMENT_EXCEPTIONS = frozenset(
    {"ModuleNotFoundError", "ImportError", "SyntaxError", "IndentationError", "NameError"}
)

TRACEBACK_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<function>\S+)')
FINAL_LINE = re.compile(r"^(?P<exception>[A-Za-z_][\w.]*)(?::\s*(?P<message>.*))?$", re.MULTILINE)


class ReproOutcome(StrEnum):
    """What stage 1 established."""

    CONFIRMED = "confirmed"
    NO_CRASH = "no_crash"
    MISSING_INPUT = "missing_input"
    ENVIRONMENT = "environment"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class RawRepro:
    """The result of running the reported input."""

    outcome: ReproOutcome
    exception: str | None = None
    message: str = ""
    path: str | None = None
    function: str | None = None
    line: int | None = None
    traceback: str = ""
    origin: str = ""
    code: str = ""

    @property
    def reproduced(self) -> bool:
        """Whether the reported input really crashes."""
        return self.outcome is ReproOutcome.CONFIRMED

    @property
    def reason(self) -> Reason | None:
        """The decline reason this outcome maps to."""
        return {
            ReproOutcome.NO_CRASH: Reason.REPRO_NO_CRASH,
            ReproOutcome.MISSING_INPUT: Reason.REPRO_MISSING_INPUT,
            ReproOutcome.ENVIRONMENT: Reason.REPRO_ENVIRONMENT,
            ReproOutcome.TIMED_OUT: Reason.REPRO_ENVIRONMENT,
        }.get(self.outcome)


def parse_traceback(output: str) -> tuple[str | None, str, list[dict[str, Any]]]:
    """Return the exception, its message, and the frames from captured stderr."""
    frames = [
        {
            "path": normalise_path(match.group("file")),
            "line": int(match.group("line")),
            "function": match.group("function"),
        }
        for match in TRACEBACK_FRAME.finditer(output)
    ]

    exception = message = None
    for match in FINAL_LINE.finditer(output):
        name = match.group("exception")
        if EXCEPTION_PATTERN.fullmatch(name) or name.endswith(("Error", "Exception")):
            exception, message = name, (match.group("message") or "").strip()
    return exception, message or "", frames


def project_frame(
    frames: list[dict[str, Any]], *, script: str = SCRIPT_PATH
) -> dict[str, Any] | None:
    """Return the innermost frame belonging to the project.

    The generated script is itself a frame, and the standard library contributes
    several. Neither is where the bug lives.
    """
    for frame in reversed(frames):
        path = str(frame["path"])
        if path.endswith(script):
            continue
        if "/lib/python" in path or path.startswith("<"):
            continue
        return frame
    return None


async def run(
    *,
    sandbox: Any,  # noqa: ANN401 - a SandboxPort
    worktree_path: Path,
    report_text: str,
    package: str,
    timeout_seconds: int | None = None,
) -> RawRepro:
    """Extract the reported input, run it in the sandbox, and classify the result.

    Args:
        sandbox: The sandbox to run in.
        worktree_path: A throwaway copy of the target repository.
        report_text: The reporter's own words.
        package: The importable package name under test.
        timeout_seconds: Overrides the sandbox timeout.

    Returns:
        What stage 1 established, including the real traceback when there is one.
    """
    extracted = repro_input.from_report(report_text)
    if extracted is None:
        return RawRepro(outcome=ReproOutcome.MISSING_INPUT)

    script = repro_input.build_script(extracted, package=package)
    wt.write_file(worktree_path, SCRIPT_PATH, script)

    result = await sandbox.run(
        worktree=str(worktree_path),
        command=["python", SCRIPT_PATH],
        timeout_seconds=timeout_seconds,
    )

    if result.get("timed_out"):
        return RawRepro(
            outcome=ReproOutcome.TIMED_OUT, origin=extracted.origin, code=extracted.code
        )

    if result["exit_code"] == 0:
        return RawRepro(
            outcome=ReproOutcome.NO_CRASH,
            traceback=str(result.get("stderr", "")),
            origin=extracted.origin,
            code=extracted.code,
        )

    stderr = str(result.get("stderr", ""))
    exception, message, frames = parse_traceback(stderr)

    if exception in ENVIRONMENT_EXCEPTIONS:
        return RawRepro(
            outcome=ReproOutcome.ENVIRONMENT,
            exception=exception,
            message=message,
            traceback=stderr,
            origin=extracted.origin,
            code=extracted.code,
        )

    frame = project_frame(frames)
    return RawRepro(
        outcome=ReproOutcome.CONFIRMED,
        exception=exception,
        message=message,
        path=str(frame["path"]) if frame else None,
        function=str(frame["function"]) if frame else None,
        line=int(frame["line"]) if frame else None,
        traceback=stderr,
        origin=extracted.origin,
        code=extracted.code,
    )
