# 0010. Python 3.12, uv, src layout, and a strict toolchain

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

The project must be reproducible on a clean machine, trivially auditable, and
strict enough that the agent's own code is held to the standard it applies to the
code it patches.

## Decision drivers

- A reviewer must be able to reproduce the environment exactly
- Type errors and lint failures must block, not warn
- The target repositories are Python, so the sandbox and the agent share an ecosystem

## Considered options

1. Python with pip and requirements files
2. Python with Poetry
3. Python 3.12 with uv, a committed lock file, and a src layout

## Decision

Option 3. `uv` pins the interpreter and resolves a committed `uv.lock`.
The `src/` layout guarantees tests import the installed package rather than the
working directory. `ruff` covers formatting, linting, import order, docstrings,
annotations, and the bandit security rules. `mypy --strict` runs over source,
tests, and the evaluation harness alike.

## Consequences

**Good:**

- A clean machine reproduces the environment from the lock file alone
- The src layout catches packaging mistakes that a flat layout hides until release
- One tool for format and lint keeps pre-commit fast

**Bad:**

- Python 3.12 exactly, so 3.13 features are unavailable until the pin moves
- Strict typing costs time on adapter boundaries, where third-party stubs are incomplete

**Neutral:**

- uv is young, and it is a development dependency only; the published package is standard

## Rejected options and why

### pip with requirements files

No lock by default, slow, and it does not manage the interpreter, so the reviewer
and the author routinely run different Pythons.

### Poetry

Capable, and slower, with a history of resolver behaviour that complicates
reproducible CI. uv covers the same ground and also installs the interpreter.
