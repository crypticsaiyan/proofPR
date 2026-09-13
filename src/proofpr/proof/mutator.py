"""Diff-scoped AST mutation.

ADR 0005. A test that fails before a patch and passes after can still be
vacuous: it may assert almost nothing, or assert something the patch changed
incidentally. Mutation answers a sharper question. If the patched lines are
altered in a small, plausible way and the new test still passes, the test is not
actually checking the fix.

Only lines the patch touched are mutated. That keeps the check proportional to
the change, keeps it inside the run budget, and keeps the report about this fix
rather than about the module's general test coverage. The operator set is fixed
and applied in a fixed order, so the same patch always produces the same mutants
and the proof is reproducible by anyone.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: Comparison flips. Each maps an operator to the nearest wrong one, which is
#: the mistake a patch is most likely to have made in the first place.
COMPARISON_FLIPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

BOOLEAN_FLIPS: dict[type[ast.boolop], type[ast.boolop]] = {ast.And: ast.Or, ast.Or: ast.And}


@dataclass(frozen=True, slots=True)
class Mutant:
    """One altered version of a file, and what was altered."""

    code: str
    operator: str
    line: int
    description: str

    @property
    def label(self) -> str:
        """A short human-readable identity, used in the proof block."""
        return f"line {self.line}: {self.description}"


class _Mutation(ast.NodeTransformer):
    """Applies exactly one mutation, identified by node position and operator."""

    def __init__(self, target: int, operator: str, lines: frozenset[int]) -> None:
        """Build a transformer that mutates only the nth eligible site."""
        self.target = target
        self.operator = operator
        self.lines = lines
        self.seen = -1
        self.applied = False
        self.line = 0
        self.description = ""

    def _eligible(self, node: ast.AST, operator: str) -> bool:
        """Return whether this node is the site we were asked to mutate."""
        if operator != self.operator:
            return False
        line = getattr(node, "lineno", None)
        if line is None or line not in self.lines:
            return False
        self.seen += 1
        return self.seen == self.target

    def visit_Compare(self, node: ast.Compare) -> ast.AST:  # ast.NodeTransformer API
        """Flip a comparison operator."""
        self.generic_visit(node)
        for index, op in enumerate(node.ops):
            replacement = COMPARISON_FLIPS.get(type(op))
            if replacement is None:
                continue
            if self._eligible(node, "comparison"):
                node.ops[index] = replacement()
                self.applied = True
                self.line = node.lineno
                self.description = f"{type(op).__name__} becomes {replacement.__name__}"
                break
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:  # ast.NodeTransformer API
        """Swap `and` for `or`, and back."""
        self.generic_visit(node)
        replacement = BOOLEAN_FLIPS.get(type(node.op))
        if replacement is not None and self._eligible(node, "boolean"):
            node.op = replacement()
            self.applied = True
            self.line = node.lineno
            self.description = f"{type(node.op).__name__} becomes {replacement.__name__}"
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # ast.NodeTransformer API
        """Shift a numeric boundary or flip a boolean."""
        if isinstance(node.value, bool):
            if self._eligible(node, "boolean_constant"):
                self.applied = True
                self.line = node.lineno
                self.description = f"{node.value} becomes {not node.value}"
                return ast.copy_location(ast.Constant(value=not node.value), node)
        elif isinstance(node.value, int) and self._eligible(node, "boundary"):
            self.applied = True
            self.line = node.lineno
            self.description = f"{node.value} becomes {node.value + 1}"
            return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node

    def visit_Raise(self, node: ast.Raise) -> ast.AST:  # ast.NodeTransformer API
        """Remove a raise, which is how most validation fixes are written."""
        if self._eligible(node, "raise_removal"):
            self.applied = True
            self.line = node.lineno
            self.description = "the raise is removed"
            return ast.copy_location(ast.Pass(), node)
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:  # ast.NodeTransformer API
        """Return None instead of the computed value."""
        if node.value is not None and self._eligible(node, "return_removal"):
            self.applied = True
            self.line = node.lineno
            self.description = "the returned value becomes None"
            return ast.copy_location(ast.Return(value=ast.Constant(value=None)), node)
        return node


#: Applied in this order, so the cheapest and most revealing mutants come first.
#:
#: String literals are deliberately absent. Emptying one usually produces an
#: equivalent mutant: a docstring, or the text of an error message no test
#: asserts on. Since the proof requires every mutant to die, including operators
#: that generate unkillable mutants would block honest patches and make the kill
#: ratio meaningless. An operator earns its place only if surviving it is
#: genuinely evidence of a weak test.
OPERATORS = (
    "comparison",
    "raise_removal",
    "boundary",
    "boolean",
    "boolean_constant",
    "return_removal",
)


def changed_lines(before: str, after: str) -> frozenset[int]:
    """Return the line numbers in `after` that differ from `before`.

    A whole-file comparison rather than a real diff, because the patch is applied
    as a file replacement and the lines that matter are exactly the ones that
    changed. Added lines count; removed lines have no line to mutate.
    """
    import difflib

    old_lines = before.splitlines()
    new_lines = after.splitlines()
    changed: set[int] = set()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, _, _, start, end in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            changed.update(range(start + 1, end + 1))
    return frozenset(changed)


def generate(source: str, lines: frozenset[int], *, limit: int = 12) -> list[Mutant]:
    """Produce one mutant per eligible site on the given lines.

    Args:
        source: The patched file.
        lines: Line numbers the patch touched.
        limit: Maximum mutants. A patch large enough to exceed this is outside
            the fix class anyway, and the run budget matters more than exhausting
            a diff nobody should have written.

    Returns:
        Mutants in operator order, each a complete file.
    """
    if not lines:
        return []
    try:
        ast.parse(source)
    except SyntaxError:
        return []

    mutants: list[Mutant] = []
    for operator in OPERATORS:
        index = 0
        while len(mutants) < limit:
            tree = ast.parse(source)
            mutation = _Mutation(index, operator, lines)
            mutated = mutation.visit(tree)
            if not mutation.applied:
                break
            ast.fix_missing_locations(mutated)
            try:
                code = ast.unparse(mutated)
            except (AttributeError, ValueError):  # pragma: no cover - unparse is total in 3.12
                break
            if code != ast.unparse(ast.parse(source)):
                mutants.append(
                    Mutant(
                        code=code,
                        operator=operator,
                        line=mutation.line,
                        description=mutation.description,
                    )
                )
            index += 1
    return mutants[:limit]
