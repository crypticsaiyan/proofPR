"""Case and result schemas for the evaluation harness.

A case is data plus ground truth. The ground truth fields are what make a result
a measurement rather than an anecdote, so they are required rather than optional
and are validated when the dataset loads, not when a run fails halfway through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Intent = Literal["bug", "question", "feature", "chatter"]

#: A dataset directory holding this file describes faults to inject, not cases.
FAULT_MATRIX_NAME = "matrix.yaml"


class SeededIssue(BaseModel):
    """An issue to seed into an application before a case runs."""

    model_config = ConfigDict(extra="forbid")

    app: Literal["linear", "github"]
    title: str
    body: str = ""
    state: str = "open"


class Case(BaseModel):
    """One evaluation case with its ground truth."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    author: str = "reporter"
    source: str = "fixture"

    # Ground truth, per AGENTS.md section 12.1.
    intent: Intent = "bug"
    duplicate_of: str | None = None
    already_fixed_in: str | None = None
    fix_in_flight_pr: str | None = None
    in_scope: bool = True
    reproducible: bool | None = None
    answer_if_asked: str | None = None
    expected_outcome: str
    expected_reason: str | None = None

    seed: list[SeededIssue] = Field(default_factory=list)
    notes: str = ""

    # Target repository, for cases that are not fixture-based (real_bugs). A
    # case that sets these is not runnable through the single shared
    # `--repo`/`--package` flag on `proofpr eval`; the runner reads them only
    # once it grows per-case repository support (tracked in
    # docs/TARGET_REPO.md). None of the seeded or injection cases set them.
    repo_url: str | None = None
    buggy_commit: str | None = None
    fixed_commit: str | None = None
    package: str | None = None
    #: Path (from the repo root) pytest should run the hidden test at.
    #: Defaults to the convention every other dataset uses; real_bugs cases
    #: whose real test suite does not live under a top-level `tests/`
    #: (tqdm keeps its own under `tqdm/tests/`) override it.
    hidden_test_path: str = "tests/test_hidden_maintainer.py"

    @classmethod
    def load(cls, path: Path) -> Case:
        """Load one case from a YAML file."""
        document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(document)

    @classmethod
    def load_all(cls, directory: Path) -> list[Case]:
        """Load every case in a directory, sorted by identifier.

        `matrix.yaml` is a fault matrix rather than a case, and is skipped.
        """
        cases = [
            cls.load(path)
            for path in sorted(directory.glob("*.yaml"))
            if path.name != FAULT_MATRIX_NAME
        ]
        duplicates = {case.id for case in cases if [c.id for c in cases].count(case.id) > 1}
        if duplicates:
            raise ValueError(f"duplicate case ids in {directory}: {sorted(duplicates)}")
        return sorted(cases, key=lambda case: case.id)
