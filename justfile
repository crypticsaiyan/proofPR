# ProofPR task runner. Requires `just` and `uv`.
set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Create the virtualenv and install all dependencies.
install:
    uv sync --all-extras
    uv run pre-commit install

# Format and autofix.
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Lint without changing files.
lint:
    uv run ruff format --check .
    uv run ruff check .

# Static types.
types:
    uv run mypy

# Unit, property and contract tests. Live tests excluded.
test *ARGS:
    uv run pytest -m "not live" {{ARGS}}

# Tests that hit real apps. Requires credentials.
test-live:
    uv run pytest -m live

# Dependency and secret scanning.
audit:
    uv run pip-audit
    gitleaks detect --no-banner --redact

# Everything CI runs.
check: lint types test

# One verified write and readback per app, plus a sandbox suite run.
doctor:
    uv run proofpr doctor

# Start the Discord gateway bot. Needs DISCORD_BOT_TOKEN and a guild.
bot:
    uv run proofpr-bot

# Evaluate both arms on one split. Needs OPENROUTER_API_KEY and a sandbox image.
eval split="dev" arms="both":
    uv run proofpr eval --split {{split}} --arms {{arms}}

# Same, with fixed answers instead of a model. Measures the harness, not a model.
eval-scripted split="dev":
    uv run proofpr eval --split {{split}} --model scripted

# Tests that need a docker daemon and the sandbox image.
test-docker *ARGS:
    uv run pytest -m docker {{ARGS}}

# Regenerate results.md and report.html from run ledgers only.
report:
    uv run proofpr report

# Build the application image.
build:
    docker build -t proofpr:local .

# Build the pinned sandbox runner image.
sandbox-image:
    docker build -t proofpr-sandbox:local sandbox/

# Pin every real_bugs case's target repository to its real buggy commit.
real-bugs-fetch:
    uv run python eval/fetch_real_bugs.py

# Build every real_bugs case's sandbox image (one per case: see
# sandbox/real_bugs/<case-id>/Dockerfile for why it isn't one per library).
real-bugs-images:
    for d in sandbox/real_bugs/*/; do \
        id=$(basename "$d"); \
        docker build -t "proofpr-real-bugs-$id:local" "$d"; \
    done
