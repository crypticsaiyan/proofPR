# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- The Sentry integration: adapter, enrichment step (replaced by a plain
  `fingerprint` step), Sentry-captured-locals reproduction input, Sentry notes,
  the Sentry leg of the consistency assertion (now three-way: Discord, GitHub,
  Linear), the regression watcher, the `/webhooks/sentry` endpoint, its policy
  section, and its settings.

### Added

- Project scaffold: `uv` toolchain, `src/` layout, ruff, mypy strict, pytest with
  coverage, pre-commit, and a `justfile` task runner (M0).
- Domain vocabulary: pipeline steps, terminal outcomes, decline reason codes, and
  evaluation arms, pinned by tests because they are a persisted wire format.
- Typed settings with `SecretStr` for every credential.
- Structured JSON logging with run and trace identifiers.
- CLI surface: `doctor`, `run`, `eval`, `report`, `reconcile`, `verify-receipt`.
  Unbuilt commands name their milestone and exit non-zero.
- Continuous integration: lint, types, tests, container build, secret scanning,
  dependency audit, CodeQL, and documentation invariants.
- Operation allowlist and sandbox runner image definitions.
- Documentation set and the first ten architecture decision records.
- Ports for the four applications, the model, and the sandbox, with in-memory
  fakes that go through the same guard as the real adapters (M1).
- Adapters for Discord, GitHub, Linear, and Sentry: retries with jittered
  backoff, `Retry-After` handling, deterministic fault injection, error mapping
  onto the domain hierarchy, run markers on every written body, and readback
  verification after every write.
- The guard: allowlist loaded from `config/policy.yaml` with per-operation
  constraints, diff rules, an egress scanner covering the canary, credential
  shapes and environment secret values, and the untrusted content sanitizer.
- Durable SQLite ledger in WAL mode: append-only events, hash chain, receipt,
  resume point, idempotent write records, and per-run cost accounting.
- Docker sandbox runner with the full isolation flag set, output truncation, and
  worktree permission preparation.
- `proofpr doctor`: one real write and readback per application, a sandbox test
  run under full isolation, and a ledger write with chain verification. Reports
  every application in one pass and exits non-zero unless all checks pass.
- Contract tests asserting the adapters and `config/policy.yaml` describe exactly
  the same set of writes, in both directions.
- The triage pipeline (M2): a durable state machine sequencing sanitize,
  pre-check, Sentry enrichment, the exists check, and the worth-it rules, with a
  ledger snapshot after every step.
- Deterministic triage rules: stack fingerprinting with path normalisation,
  PEP 440 subset version parsing and support windows, intent classification whose
  rules stop only what they are certain of, and the rules-only scope decision.
- The exists check across Linear, GitHub and Sentry, with `duplicate`,
  `already_fixed` and `fix_in_flight` outcomes. `already_fixed` requires a
  verbatim symbol or error match in a commit between the reporter's version and
  HEAD; a model opinion is never sufficient.
- OpenRouter model client: schema-validated JSON output, one corrective retry,
  per-call usage and cost, and a prompt version hash recorded on every run. No
  tool definitions are ever sent.
- In-memory applications in the package, so `proofpr run --dry-run` and the CI
  smoke job exercise the real guard, ledger and pipeline with no credentials.
- `proofpr run <fixture-or-discord-link>`, with `--dry-run`.
- Evaluation case schema with ground truth fields, the first seeded cases, and a
  contract test that fails the build when ground truth contradicts itself.
- Two-stage reproduction (M3). Stage 1 extracts something runnable from the
  report (a fenced block, the traceback's own call line, an inline call, or a
  Sentry event's captured locals) and runs it in the sandbox, capturing the real
  traceback. Stage 2 turns that traceback into a single pytest test.
- The reproduction gate: the suite must be green on base, the test must collect,
  it must fail, and it must fail with the exception stage 1 actually captured or
  with an assertion. Each condition is recorded individually.
- Structural validation of generated tests before they are ever run: one test
  function, under `tests/`, parses, asserts something, and imports nothing beyond
  pytest and the package under test.
- The clarify path: a report that does not reproduce gets one precise question,
  and the run ends rather than blocking. Works with no model configured.
- `confirmed_no_test`: a crash that was reproduced but could not be expressed as
  a test in three attempts is filed with its traceback rather than discarded.
- Throwaway worktrees for every sandbox run, so nothing a run does touches the
  real checkout.
- A sample target repository (`tests/fixtures/validkit`) with a real bug, and
  docker-marked tests that reproduce it end to end in a real container.
- The patch loop (M4): three attempts, each shown the previous failure output,
  with every proposal checked against the same allowlist that governs a push
  before it is applied. A patch may touch one file, may not create files, and may
  not traverse out of the repository.
- The three proof checks. Revert puts the bug back and requires the test to fail
  again. Flake requires three consecutive passes. Mutation alters the patched
  lines with a fixed, ordered operator set and requires every mutant to be
  killed.
- The proof block: a table where every row is a command a reviewer can rerun,
  attached to the Linear issue and carrying the run marker.
- A patch that passes its test and the suite but fails any proof check is
  abandoned rather than published, and says which check failed.
- Publication (M5): the approval gate, branch creation, the test committed before
  the fix, and a draft pull request whose body leads with the proof block.
- Approval as a port with three configurations: automatic for evaluation, a
  configured opt-out for a deployment that trusts the proof, and refusal by
  default. An approver that errors or cannot be reached is a refusal, never
  consent.
- CI readback from the checks API, with polling and a timeout. A pull request
  leaves draft only on a green conclusion; no checks at all is never success.
- The four-way consistency assertion across GitHub, Linear, Sentry and Discord,
  recorded per application, run for every terminal state including the ones that
  write no code.
- The pull request attached to the Linear issue, and the reporter told where it
  is and what CI said.
- An end-to-end docker test taking one report to a pull request with real
  containers, real mutants, and a real proof block.
- Durability (M6). `proofpr resume` continues a run from the last step that
  finished, skipping completed steps and re-entering the one that died. Re-entry
  writes nothing twice, because every write is recorded before it is attempted
  and skipped when it already succeeded.
- Crash tests at all nine steps after sanitizing, each asserting the same final
  outcome, one issue, one branch, one pull request, and a chain that still
  verifies.
- A real SIGKILL test: a separate process is killed outright mid-run and the
  ledger is inspected from another process. Every event written before the kill
  survives, the in-flight step is identified, and the next writer proceeds with
  no recovery pause.
- Fault injection tests across all four adapters, covering 429, 5xx and timeouts,
  asserting that a transient fault costs latency rather than correctness and that
  a fault on a write leaves at most one write behind.
- The receipt is now sealed as the chain head before the closing event, which is
  what makes it printable in the pull request, the issue and the thread. It is
  recorded inside the closing event as well, so a forged receipt is detectable.
- `proofpr verify-receipt <run-id>`, with `--show-events` to print the chain.

- Post-run loops (M7). The release notifier tells the reporter, the issue, and
  Sentry when a proof-carrying pull request merges, identified by the run marker
  in the pull request's own body. Sentry is resolved only here, after a merge,
  which is the one place the allowlist permits a status change at all.
- The regression watcher notices when a shipped fix stops holding, matching on
  the stack fingerprint recorded at triage rather than on issue identity, and
  reopens the issue and tells the reporter instead of quietly ignoring it.
- The drift reconciler re-runs the four-way assertion over recent finished runs
  and repairs what it safely can. It re-applies conclusions the run already
  reached and never reaches new ones: a missing pull request is left for a human
  because re-opening one would be a new decision.
- The webhook service: `/webhooks/github`, `/webhooks/sentry`, `/webhooks/linear`,
  plus `/healthz` and `/readyz`. Every request is verified against the raw body in
  constant time, and an unconfigured secret refuses everything rather than
  skipping the check.
- `proofpr reconcile`, the same sweep the service runs every six hours, exiting
  non-zero when a divergence needs a human.
- A `state_final` snapshot written after the terminal writes, so the reconciler
  and the report see the issue, the reply, and the attachment rather than a state
  that stops before the run did.

### Changed

- The shipped configuration and the operation allowlist moved into the package
  (`src/proofpr/defaults/`), and the ledger migrations with them
  (`src/proofpr/ledger/migrations/`). Both were previously resolved from the
  working directory, so `proofpr` only worked when started from a checkout.
  `config/` is now for local overrides, which take precedence.

[Unreleased]: https://github.com/OWNER/proofpr/commits/main
