# Local configuration

The shipped defaults live inside the package, in `src/proofpr/defaults/`:

| File | What it is |
|---|---|
| `proofpr.toml` | Every non-secret setting, commented |
| `policy.yaml` | The operation allowlist, which is the statement of blast radius |

They are packaged rather than read from the working directory, so `proofpr` works
wherever it is started from rather than only in a checkout.

To override either, copy it here and edit the copy:

```bash
cp src/proofpr/defaults/proofpr.toml config/proofpr.toml
cp src/proofpr/defaults/policy.yaml config/policy.yaml
```

Files in this directory take precedence over the packaged defaults. Secrets never
belong in either; those live in `.env`. See `docs/CONFIGURATION.md`.

A local `policy.yaml` may tighten the allowlist freely. Widening it requires an
ADR and a CODEOWNERS review on the packaged default, because that file is the
answer to "what can this thing do".
