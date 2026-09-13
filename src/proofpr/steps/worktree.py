"""Worktrees: the copy of the target repository a sandbox run sees.

Every sandbox invocation gets its own throwaway directory. Nothing the model
wrote is ever executed against the real checkout, and nothing a run writes
survives it, which makes an escaped side effect structurally impossible rather
than merely unlikely.

The directory is mounted read-only, so a test that needs to write must write
under `/tmp`, which is the tmpfs the sandbox provides.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from proofpr.adapters.docker_sandbox import prepare_worktree
from proofpr.domain.errors import SandboxError

#: Never copied into a worktree: large, irrelevant, or a place a secret hides.
EXCLUDED = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "*.pyc",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".env",
    ".env.*",
    "node_modules",
    "*.db",
    "*.sqlite3",
)


@contextmanager
def worktree(source: Path, *, prefix: str = "proofpr-run-") -> Iterator[Path]:
    """Copy a repository into a throwaway directory and yield its path.

    Args:
        source: The checkout to copy.
        prefix: Temporary directory prefix, so a leaked directory is traceable.

    Yields:
        The worktree path, already readable by the sandbox user.

    Raises:
        SandboxError: The source is not a directory.
    """
    if not source.is_dir():
        raise SandboxError(f"target repository not found: {source}")

    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        destination = Path(directory) / source.name
        shutil.copytree(source, destination, ignore=EXCLUDED, symlinks=False)
        prepare_worktree(Path(directory))
        yield destination


def write_file(root: Path, relative: str, content: str) -> Path:
    """Write a file into a worktree and make it readable by the sandbox user.

    Args:
        root: The worktree.
        relative: Path within it. Must stay inside it.
        content: File contents.

    Returns:
        The written path.

    Raises:
        SandboxError: The path escapes the worktree.
    """
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise SandboxError(f"refusing to write outside the worktree: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o644)
    return target


def revert_file(root: Path, relative: str, original: str | None) -> None:
    """Restore a file to its previous contents, or remove it when it was new.

    Used by the revert check in M4, and by reproduction whenever a candidate test
    must be taken back out before the suite is measured again.
    """
    target = root / relative
    if original is None:
        target.unlink(missing_ok=True)
        return
    target.write_text(original, encoding="utf-8")
    target.chmod(0o644)


def read_file(root: Path, relative: str) -> str | None:
    """Return a file's contents, or None when it does not exist."""
    target = root / relative
    return target.read_text(encoding="utf-8") if target.is_file() else None


def find_source_file(root: Path, path_hint: str) -> Path | None:
    """Locate the source file a stack frame names, inside a worktree.

    A frame gives a path as it existed on the reporter's machine. The same file
    in the worktree may sit under a different prefix, so the tail of the path is
    matched rather than the whole of it.
    """
    candidate = root / path_hint
    if candidate.is_file():
        return candidate
    for prefix in ("src", "."):
        candidate = root / prefix / path_hint
        if candidate.is_file():
            return candidate

    tail = path_hint.split("/")[-1]
    matches = sorted(
        (match for match in root.rglob(tail) if match.is_file()),
        key=lambda match: len(match.parts),
    )
    return matches[0] if matches else None
