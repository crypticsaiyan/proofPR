# Contributing

[AGENTS.md](AGENTS.md) is the contract. Read it before opening a pull request; it
outranks habit, preference, and anything written here.

## Setup

```bash
just install     # uv sync --all-extras, then pre-commit install
just check       # what CI runs: lint, types, tests
```

Python 3.12 exactly. `uv` manages the interpreter, so no system Python is
required.

## Branching and commits

- Branch from `main`. Name branches `feat/…`, `fix/…`, `docs/…`, `chore/…`.
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/),
  enforced by a `commit-msg` hook.
- Keep commit messages to the point. One subject line plus a short body when the
  change is not obvious.

## Rules that are not negotiable

1. **The model never gets tools.** If a change lets a model choose an operation,
   it is rejected regardless of how well it works.
2. **No new allowed operation without an ADR.** `src/proofpr/defaults/policy.yaml` tightens
   freely and widens only with a recorded decision and a CODEOWNERS review.
3. **No evaluation number typed by hand.** Every figure in the README, the
   reliability brief, and the demo is produced by `proofpr report` from run
   ledgers.
4. **No em dashes.** Use commas, colons, semicolons, or separate sentences. CI
   fails the build on one.
5. **Tests accompany behaviour**, including the failure path. A step that can
   decline needs a test that shows it declining for the right reason.

## Architecture decision records

Decisions live in `docs/adr/` in [MADR](https://adr.github.io/madr/) format.

```bash
cp docs/adr/0000-template.md docs/adr/00NN-short-title.md
```

Record the alternatives you rejected and why. An ADR that lists no rejected
option is not a decision, it is a description.

## Reviews

Pull requests need `just check` green, the checklist in the template completed,
and evidence pasted: test output, a ledger excerpt, or a report row.
