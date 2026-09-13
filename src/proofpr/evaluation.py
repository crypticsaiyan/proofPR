"""The evaluation entry point used by the CLI.

Thin on purpose. The datasets, the arms, the runner, and the report generator
live under `eval/` because they are the experiment rather than the product, and
this module is the seam that lets `proofpr eval` and `proofpr report` reach them
without the package depending on the experiment.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from proofpr.domain.errors import ConfigurationError

#: The experiment lives beside the package in a checkout, and is absent from an
#: installed wheel. That is deliberate: shipping the datasets would put the
#: hidden tests one import away from a prompt.
EVAL_DIR = Path("eval")


@dataclass(frozen=True, slots=True)
class Split:
    """A frozen dev or test assignment."""

    name: str
    case_ids: tuple[str, ...]
    frozen_at: str | None
    prompt_version_hash: str | None


def _ensure_importable(eval_dir: Path) -> None:
    """Put the experiment on the import path.

    Raises:
        ConfigurationError: There is no experiment here.
    """
    if not (eval_dir / "runner.py").is_file():
        raise ConfigurationError(
            f"no evaluation harness at {eval_dir.resolve()}. "
            "The datasets and runner live in a checkout, not in an installed package."
        )
    path = str(eval_dir.resolve())
    if path not in sys.path:
        sys.path.insert(0, path)


def load_split(name: str, *, eval_dir: Path = EVAL_DIR) -> Split:
    """Load a frozen split by name.

    Raises:
        ConfigurationError: The split file is missing or names no such split.
    """
    path = eval_dir / "splits.yaml"
    if not path.is_file():
        raise ConfigurationError(f"no splits file at {path}")
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    if name not in document or not isinstance(document.get(name), list):
        raise ConfigurationError(f"no split named {name!r} in {path}")
    return Split(
        name=name,
        case_ids=tuple(str(case_id) for case_id in document[name]),
        frozen_at=document.get("frozen_at"),
        prompt_version_hash=document.get("prompt_version_hash"),
    )


def _all_cases(eval_dir: Path) -> dict[str, Any]:
    """Load every case from every dataset, keyed by identifier."""
    _ensure_importable(eval_dir)
    from schema import (
        Case,
    )  # imported here: the experiment is on the path only after _ensure_importable

    found: dict[str, Any] = {}
    for directory in sorted((eval_dir / "datasets").iterdir()):
        if directory.is_dir():
            for case in Case.load_all(directory):
                found[case.id] = case
    return found


def load_cases(split: Split, *, eval_dir: Path = EVAL_DIR) -> list[Any]:
    """Load every case in a split, from every dataset.

    Raises:
        ConfigurationError: A case named in the split does not exist.
    """
    found = _all_cases(eval_dir)
    missing = [case_id for case_id in split.case_ids if case_id not in found]
    if missing:
        raise ConfigurationError(
            f"the {split.name} split names cases that do not exist: {', '.join(missing)}"
        )
    return [found[case_id] for case_id in split.case_ids]


def load_case(case_id: str, *, eval_dir: Path = EVAL_DIR) -> Any:  # noqa: ANN401
    """Load a single case by identifier, from any dataset.

    Used by the fault matrix, which names one fixed publishable case rather than
    a split.

    Raises:
        ConfigurationError: No dataset defines a case with this identifier.
    """
    found = _all_cases(eval_dir)
    if case_id not in found:
        raise ConfigurationError(f"no case {case_id!r} in any dataset under {eval_dir}")
    return found[case_id]


def datasets_root(eval_dir: Path = EVAL_DIR) -> Path:
    """Return the directory holding every dataset."""
    return eval_dir / "datasets"


def build_scripted_model_factory(*, eval_dir: Path = EVAL_DIR) -> Any:  # noqa: ANN401
    """Return a factory producing the scripted stand-in for the model.

    For running the harness without credentials. What comes out measures the
    harness, never the model, and every report built from it says so.
    """
    _ensure_importable(eval_dir)
    from scripted import (
        ScriptedModel,
    )  # imported here: the experiment is on the path only after _ensure_importable

    return ScriptedModel


def build_fault_runner(
    runner: Any,  # noqa: ANN401 - the experiment's Runner
    case: Any,  # noqa: ANN401 - the experiment's Case
    *,
    eval_dir: Path = EVAL_DIR,
) -> Any:  # noqa: ANN401 - the experiment's FaultRunner
    """Construct the fault runner over an existing case runner.

    Raises:
        ConfigurationError: The fault matrix is missing.
    """
    _ensure_importable(eval_dir)
    from faults import (
        FaultMatrix,
        FaultRunner,
    )  # imported here: the experiment is on the path only after _ensure_importable

    matrix_path = eval_dir / "datasets" / "faults" / "matrix.yaml"
    if not matrix_path.is_file():
        raise ConfigurationError(f"no fault matrix at {matrix_path}")
    return FaultRunner(runner, FaultMatrix.load(matrix_path), case)


def build_runner(
    *,
    settings: Any,  # noqa: ANN401 - a Settings
    ledger: Any,  # noqa: ANN401 - a Ledger
    repo_path: Path,
    package: str,
    model_factory: Any,  # noqa: ANN401 - takes a Case, returns a ModelPort
    datasets_root: Path,
    eval_dir: Path = EVAL_DIR,
) -> Any:  # noqa: ANN401 - the experiment's Runner
    """Construct the runner from the experiment directory."""
    _ensure_importable(eval_dir)
    from runner import (
        Runner,
    )  # imported here: the experiment is on the path only after _ensure_importable

    return Runner(
        settings=settings,
        ledger=ledger,
        repo_path=repo_path,
        package=package,
        model_factory=model_factory,
        datasets_root=datasets_root,
    )


def write_report(
    ledger: Any,  # noqa: ANN401 - a Ledger
    *,
    out_dir: Path,
    split: str,
    prompt_version: str | None = None,
    eval_dir: Path = EVAL_DIR,
) -> dict[str, Path]:
    """Generate results.md, report.html, and results.json from the ledger."""
    _ensure_importable(eval_dir)
    from report import (
        write,
    )  # imported here: the experiment is on the path only after _ensure_importable

    result: dict[str, Path] = write(
        ledger, out_dir=out_dir, split=split, prompt_version=prompt_version
    )
    return result


def check_clean(ledger: Any, *, eval_dir: Path = EVAL_DIR) -> Sequence[str]:  # noqa: ANN401
    """Return the problems that should fail a build."""
    _ensure_importable(eval_dir)
    from report import (
        assert_clean,
    )  # imported here: the experiment is on the path only after _ensure_importable

    problems: Sequence[str] = assert_clean(ledger)
    return problems
