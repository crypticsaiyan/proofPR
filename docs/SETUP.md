# Setup

> Status: M0 skeleton. Exact scopes are recorded here as each adapter lands in M1,
> and `proofpr doctor` is the only accepted proof that a credential works.

## Prerequisites

- `uv` (installs Python 3.12 itself)
- Docker, for the sandbox and the application image
- `just`
- `gitleaks`, for `just audit`

```bash
just install
cp .env.example .env
```

## Credentials

Each application below needs the **narrowest** credential that lets the agent do
its job. Over-scoped tokens are the most likely way this project causes harm.

| App | Credential | Scope |
|---|---|---|
| GitHub | Fine-grained PAT | Single target repository. Contents write, pull requests write, checks read. No administration, no workflows |
| Linear | API key | Single team |
| Discord | Bot token | One guild, one intake channel, no privileged intents |
| OpenRouter | API key | Spend limit set on the key itself, not only in `.env` |

`doctor` proves each of these by writing, not by reading:

| App | What `doctor` writes | What it leaves behind |
|---|---|---|
| GitHub | Creates a `proofpr/doctor-<timestamp>` branch, reads it back, deletes it | Nothing, unless deletion fails, which is reported |
| Linear | Files an issue titled "ProofPR doctor check" and reads it back | The issue. Linear has no deletion this agent may perform, and widening the allowlist for a health check would be the worst possible reason to widen it |
| Discord | Posts a message in the eval channel and reads it back | The message |

## Verification

```bash
just doctor
```

`doctor` performs one real write and reads it back for every application, then
runs the target repository suite once inside the sandbox. Anything less than a
clean run means the environment is not ready, whatever the dashboards say.
