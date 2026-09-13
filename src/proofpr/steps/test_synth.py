"""Stage 2 of reproduction: turn a captured traceback into one failing test.

Section 4.4 of AGENTS.md. The strong model is used here for the first time in a
run, and only after stage 1 has proved there is something to reproduce. It is
given the real traceback and the real source of the failing function, never the
reporter's description of either.

Its output is a file, and a file is easy to check: it must parse, it must live
under the tests directory, it must contain exactly one test function, and it must
not import anything beyond pytest and the package under test. A file failing any
of those is rejected before it runs, which is cheaper and more reliable than
discovering the problem from a sandbox stack trace.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

#: Three attempts, then the run files what it has and stops. A model that cannot
#: express a captured traceback as a test in three tries is not going to.
MAX_ATTEMPTS = 3


#: Modules a generated test may import. Everything else is a smell: a test that
#: needs the network, the filesystem, or the clock is not a reproduction.
def allowed_imports(package: str) -> frozenset[str]:
    """Return the modules a generated test may import.

    `package` may name several top-level modules, comma separated, for a project
    that keeps flat modules under `src/` rather than one importable package.
    """
    return frozenset(
        {"pytest", "datetime", "decimal", "json", "math", "re", "typing", *modules(package)}
    )


def modules(package: str) -> list[str]:
    """Split a configured package into the importable top-level modules it names."""
    return [name.strip() for name in package.split(",") if name.strip()]


TEST_NAME_PATTERN = re.compile(r"^test_[a-z0-9_]+\.py$")


class SynthesizedTest(BaseModel):
    """The model's proposed reproduction test."""

    path: str = Field(description="Path under tests/, for example tests/test_parse_date.py")
    code: str = Field(description="The complete file contents.")
    expected_exception: str = Field(
        default="", description="The exception the test asserts, empty for an assertion failure."
    )
    rationale: str = Field(default="", max_length=400)


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a proposed test was refused before it was ever run."""

    rule: str
    detail: str


def validate(test: SynthesizedTest, *, package: str, test_dir: str = "tests") -> Rejection | None:
    """Check a proposed test against the structural rules. None means accepted."""
    path = test.path.strip().lstrip("./")
    if not path.startswith(f"{test_dir}/"):
        return Rejection("path", f"tests must live under {test_dir}/, got {path!r}")
    if "/" in path[len(test_dir) + 1 :]:
        return Rejection("path", f"tests go directly in {test_dir}/, got {path!r}")
    if not TEST_NAME_PATTERN.match(path.split("/")[-1]):
        return Rejection("path", f"{path!r} is not a discoverable pytest filename")

    try:
        tree = ast.parse(test.code)
    except SyntaxError as error:
        return Rejection("syntax", f"the file does not parse: {error}")

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    if len(functions) != 1:
        return Rejection(
            "one_test", f"exactly one test function is required, found {len(functions)}"
        )

    permitted = allowed_imports(package)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for name in names:
            if name and name not in permitted:
                return Rejection("import", f"a reproduction test may not import {name!r}")

    if not _asserts_something(tree):
        return Rejection(
            "no_assertion",
            "the test asserts nothing, so it cannot fail for the right reason",
        )
    return None


def _asserts_something(tree: ast.Module) -> bool:
    """Return whether the test contains an assertion or a pytest.raises block."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.withitem):
            call = node.context_expr
            if isinstance(call, ast.Call):
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "raises":
                    return True
    return False
