# Architecture decision records

[MADR](https://adr.github.io/madr/) format. Copy `0000-template.md` to start one.
An ADR that lists no rejected option is not a decision, it is a description.

| ADR | Decision |
|---|---|
| [0001](0001-deterministic-pipeline-no-model-tools.md) | Deterministic pipeline, the model gets no tools |
| [0002](0002-tests-and-ci-as-judge.md) | Tests and CI judge the work, never a model |
| [0003](0003-durable-sqlite-ledger-and-resume.md) | Durable SQLite ledger with forward-only resume |
| [0004](0004-container-sandbox-for-generated-code.md) | Model-written code runs only in a locked-down container |
| [0005](0005-diff-scoped-ast-mutation-check.md) | Mutation testing scoped to the diff |
| [0006](0006-tighten-only-operation-allowlist.md) | A tighten-only operation allowlist in version control |
| [0007](0007-hash-chained-run-receipt.md) | Hash-chained tamper-evident run receipt |
| [0008](0008-hidden-tests-and-frozen-splits.md) | Hidden maintainer tests and splits frozen before tuning |
| [0009](0009-openrouter-model-routing.md) | Two-tier model routing through OpenRouter |
| [0010](0010-python-uv-src-layout-toolchain.md) | Python 3.12, uv, src layout, strict toolchain |
