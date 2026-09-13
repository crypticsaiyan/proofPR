# Configuration

Two files, one rule: **secrets in `.env`, everything else in
`config/proofpr.toml`.** Nothing is configured by editing source.

## Environment variables

Mirrors `.env.example`. Every secret is loaded as a `SecretStr`, so it renders as
`**********` in logs, reprs, and model prompts.

| Key | Default | Meaning |
|---|---|---|
| `PROOFPR_ENV` | `dev` | `dev`, `eval`, or `prod` |
| `PROOFPR_LOG_LEVEL` | `INFO` | Standard logging level name |
| `PROOFPR_DB_PATH` | `var/proofpr.db` | SQLite ledger path |
| `PROOFPR_ARMS` | `proofpr` | `proofpr` or `no_gate`, the baseline evaluation arm |
| `MODEL_CHEAP` | `anthropic/claude-haiku-4.5` | Intent, duplicate verdict, injection classifier |
| `MODEL_STRONG` | `anthropic/claude-opus-5` | Test synthesis and patching only |
| `MAX_RUNS` | `200` | Hard cap enforced by the runner |
| `MAX_SPEND_USD` | `25` | Hard cap enforced by the runner |
| `MAX_PATCH_ATTEMPTS` | `3` | Patch loop limit |
| `SANDBOX_TIMEOUT_SECONDS` | `60` | Per sandbox invocation |

Credential keys are listed in `.env.example` and documented in
[SETUP.md](SETUP.md).

## config/proofpr.toml

The shipped default is `src/proofpr/defaults/proofpr.toml`, and a copy at
`config/proofpr.toml` overrides it. Sections: `repo`, `fix_class`, `triage`,
`proof`, `sandbox`, `ci`, `reconciler`. Each key is commented in the example
file, which is the authoritative reference.

## policy.yaml

The operation allowlist, shipped at `src/proofpr/defaults/policy.yaml` and
overridable at `config/policy.yaml`. It may tighten at any time; widening it requires an ADR
and a CODEOWNERS review. See
[ADR 0006](adr/0006-tighten-only-operation-allowlist.md).
