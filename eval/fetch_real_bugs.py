#!/usr/bin/env python3
"""Fetch and pin the real_bugs target repositories.

For each case in `eval/datasets/real_bugs/`, this materializes a checkout of
that case's real repository (`repo_url`) at its real, pinned `buggy_commit`
under `eval/real_bugs_repos/<case-id>/` — the path `Runner.target_for` (see
`eval/runner.py`) resolves for that case at eval time.

Nothing here is committed: `eval/real_bugs_repos/` is gitignored. The six real
repositories share one clone each (under `eval/real_bugs_repos/_cache/`) and
each case gets a lightweight `git worktree` off it, so re-running this after
adding a case does not re-clone repositories already fetched.

Usage:
    uv run python eval/fetch_real_bugs.py [case-id ...]

With no arguments, fetches every real_bugs case. Safe to re-run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "eval"))

from schema import Case  # noqa: E402

DATASET_DIR = REPO_ROOT / "eval" / "datasets" / "real_bugs"
TARGET_ROOT = REPO_ROOT / "eval" / "real_bugs_repos"
CACHE_ROOT = TARGET_ROOT / "_cache"

#: Resolved once, so every git invocation below runs an absolute path rather
#: than trusting whatever "git" happens to mean on $PATH.
GIT = shutil.which("git") or "git"


def run(*args: str, cwd: Path | None = None) -> None:
    """Run a git command, echoing it, and fail loudly if it fails."""
    print(f"$ git {' '.join(args)}" + (f"  (in {cwd})" if cwd else ""))
    subprocess.run([GIT, *args], cwd=cwd, check=True)  # noqa: S603


def current_commit(worktree: Path) -> str:
    """Return the commit a worktree is currently checked out at."""
    result = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def ensure_clone(repo_url: str, package: str) -> Path:
    """Return a full local clone of `repo_url`, cloning it if needed."""
    clone_dir = CACHE_ROOT / package
    if clone_dir.is_dir():
        run("fetch", "--all", cwd=clone_dir)
        return clone_dir
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    run("clone", repo_url, str(clone_dir))
    return clone_dir


def ensure_worktree(clone_dir: Path, case_id: str, commit: str) -> None:
    """Check out `commit` for one case, as its own worktree."""
    dest = TARGET_ROOT / case_id
    if dest.is_dir():
        # Already checked out. Confirm it is pinned where it should be rather
        # than silently trusting a stale worktree from a previous case set.
        if current_commit(dest) == commit:
            print(f"{case_id}: already pinned at {commit[:12]}, skipping")
            return
        print(f"{case_id}: worktree exists but is stale, removing")
        run("worktree", "remove", "--force", str(dest), cwd=clone_dir)
    run("worktree", "add", "--detach", str(dest), commit, cwd=clone_dir)


def real_bugs_target(case: Case) -> tuple[str, str, str]:
    """Return `(repo_url, package, buggy_commit)`, or raise if incomplete.

    A case with `repo_url` set is expected to set the other two alongside it;
    `Case` itself does not enforce that (they are independently optional), so
    this is where a malformed case is caught instead of failing obscurely
    partway through a clone.
    """
    if not (case.repo_url and case.package and case.buggy_commit):
        raise ValueError(f"case {case.id} sets repo_url but not both package and buggy_commit")
    return case.repo_url, case.package, case.buggy_commit


def main(argv: list[str]) -> int:
    """Fetch and pin every real_bugs case named in `argv`, or all of them."""
    cases = [case for case in Case.load_all(DATASET_DIR) if case.repo_url is not None]
    if argv:
        wanted = set(argv)
        cases = [case for case in cases if case.id in wanted]
        missing = wanted - {case.id for case in cases}
        if missing:
            print(f"unknown real_bugs case id(s): {sorted(missing)}", file=sys.stderr)
            return 1

    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    for case in cases:
        repo_url, package, buggy_commit = real_bugs_target(case)
        clone_dir = ensure_clone(repo_url, package)
        ensure_worktree(clone_dir, case.id, buggy_commit)

    print(f"\n{len(cases)} real_bugs checkout(s) pinned under {TARGET_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
