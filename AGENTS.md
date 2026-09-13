# AGENTS.md

Operating manual for any coding agent or human contributor working on this
repository. Read it fully before changing code. When this file and the code
disagree, raise it; do not silently pick one.

---

## 1. Product

**ProofPR** turns bug reports posted in Discord into **proof-carrying pull
requests**. A PR is opened only when the agent can prove the bug: a test that
fails without the fix, passes with it, fails again when the fix is reverted, and
kills mutants of the patched lines. When it cannot prove the bug, it asks the
reporter one precise question, retries once, and otherwise files the issue with
a stated reason and never touches the code.

### 1.1 Problem

Small teams and open source projects receive bug reports where their users
already are: chat. Those reports scroll away. When they are captured, AI coding
tools happily ship patches with no evidence the bug existed or that the patch
fixes it. Maintainers are left reviewing plausible code with no proof.

Existing tools start from a GitHub issue and ship patches.
ProofPR starts where users actually complain and ships **proof**, not patches.

### 1.2 One-line pitch

"If it can't prove the bug, it won't touch your code. If it can, the PR carries
the proof."

### 1.3 Hackathon context

Multi-App AI Agent Hackathon (solo). Requirement: one useful multi-step agent
acting across at least three external apps, with evidence that it works.
Organizer confirmed: stack agnostic, headless is fine, no sponsor tools
required.

| Weight | Criterion | How ProofPR maximizes it |
|---|---|---|
| 30% | Technical execution | Durable resumable state machine; four apps with real writes and readback; webhook-driven; container sandbox; stack-trace localization; CI readback |
| 25% | Reliability and evaluation | Paired baseline-versus-full-pipeline arms on identical cases; mutation-verified reproduction gate; hidden maintainer tests on real historical bugs; held-out test split; pass^3; Wilson intervals; fault and crash injection; injection ablation; readback-confirmed unauthorized-write count |
| 20% | Usefulness | Runs on real libraries and real historical bugs, judged by the maintainers' own tests; captures chat bug reports that are otherwise lost; merges duplicate reporters; reporter-in-the-loop clarification; notifies every reporter when the fix ships |
| 15% | Originality | Refusal as the product, measured against a no-gate baseline arm; proof-carrying PRs; mutation-verified gate; regression watch; tamper-evident run receipt; tighten-only allowlist |
| 10% | Demo clarity | Thesis stated in eight seconds, then proved on a real bug, refused three ways, attacked, and measured |

---

## 2. Interfaces

ProofPR is headless. There is no web UI.

| Surface | Purpose |
|---|---|
| **Discord** | Intake (message command "Triage this", `/triage <link>`), live status message edited per step, reporter clarification in thread, maintainer Approve and Reject buttons, fix-shipped notifications |
| **GitHub** | Branch, draft PR with proof block, CI readback, merge and release webhooks |
| **Linear** | Issue of record: dedupe target, status, decline reasons, PR attachment, receipt hash |
| **CLI** | `proofpr doctor`, `proofpr run`, `proofpr eval --arm baseline|proofpr`, `proofpr report`, `proofpr reconcile`, `proofpr verify-receipt` |
| **report.html** | Static file generated from run ledgers by `proofpr report`. Not a dashboard |

### 2.1 Discord status message

```
🤖 ProofPR · run r-7f3a                               $0.06 · 71s
✅ Intake        sanitized, 0 injection flags
✅ Linear        new ENG-42 (best duplicate score 0.21)
✅ Reproduce     tests/test_parse_date_month.py FAILS (TypeError, expected ValueError)
✅ Patch         attempt 1/3 · suite 142/142
✅ Proof         revert fails ✓ · mutants killed 9/9
⏳ Approval      [ ✅ Approve PR ] [ ❌ Reject ] [ 🔍 Diff ]
```

### 2.2 PR proof block

```markdown
## Proof

| Check | Result |
|---|---|
| New test on base commit | FAILED: TypeError (expected ValueError) |
| New test with patch | PASSED 3/3 runs |
| Full suite with patch | 142 passed in 2.1s |
| Revert patch, rerun new test | FAILED (bug reintroduced is detected) |
| Mutants on patched lines | 9/9 killed |
| CI | success (link) |

Inputs: Discord report (link) · Linear ENG-42
Receipt: sha256:4be1…c09a · run r-7f3a
<!-- proofpr-run:r-7f3a -->
```

---

## 3. Architecture

### 3.1 Non-negotiable rules

1. **The model has no tools.** `pipeline.py` sequences every step and performs
   every write. Model calls return JSON validated by pydantic. Invalid output
   gets one repair retry, then the step fails closed.
2. **Tests and CI are the judge.** The model never decides whether a bug is
   reproduced, fixed, or verified. No LLM-as-judge anywhere.
3. **All app-sourced text is untrusted.** Discord messages and usernames,
   stack frames, Linear titles and comments pass
   `guard.sanitize` before reaching any prompt and are wrapped in delimited
   untrusted-content blocks.
4. **Every write passes `guard.policy.check(operation, target, payload)`.** The
   allowlist is declarative (section 6.1) and **tighten-only**: no operation
   exists that loosens repository safety.
5. **Every write is followed by a readback.** A step succeeds only when observed
   state equals intended state.
6. **Every write is idempotent.** Payloads carry a `proofpr-run:<id>` marker.
   Before writing, the adapter searches the target for the marker.
7. **Durable execution.** Every step transition is committed to the ledger
   before and after side effects. A killed process resumes from the last
   verified step.
8. **Generated code runs only in the sandbox** (section 5).
9. **Fail closed.** Ambiguity means decline with a reason, never guess.

### 3.2 Pipeline

Cheapest and most deterministic checks run first. Sandbox time and the strong
model are spent only on reports that survive every earlier stage.

```
TRIGGER  Discord message command | /triage
  -> SANITIZE          untrusted boundary, injection flags recorded
  -> PRE_CHECK  (code + cheap model, section 4.1)
       intent classification (bug | question | feature | chatter) and completeness
       not a bug report -> reply, no issue -> DONE(not_a_bug)
  -> FINGERPRINT       exception type + innermost project frame + stated version
  -> EXISTS     (section 4.2) search every connected app before doing any work
       Linear  open and recently closed issues (title terms, stack fingerprint, cheap-model verdict)
       GitHub  open issues, open PRs, commits between reporter version and HEAD
       duplicate     -> add reporter to existing issue -> reply -> VERIFY -> DONE(duplicate)
       already_fixed -> reply "fixed in <version>, please upgrade" -> VERIFY -> DONE(already_fixed)
       fix_in_flight -> reply with the open PR link -> VERIFY -> DONE(fix_in_flight)
  -> WORTH_IT   (rules only, section 4.3)
       out of scope -> Linear (label out-of-scope, reason) -> reply -> VERIFY -> DONE(out_of_scope)
  -> LOCALIZE          stack frames -> candidate files and functions
  -> REPRO_RAW  (sandbox, stage 1 of reproduction)
       run the reported input directly, capture real traceback
       no crash or missing input -> CLARIFY (ask reporter one precise question, wait)
            -> answer -> REPRO_RAW once more
            -> no answer or still no crash -> Linear (declined, reason)
               -> reply -> VERIFY -> DONE(declined)
  -> TEST_SYNTH (stage 2) strong model converts the captured traceback into one pytest test
       3 failed attempts -> Linear (confirmed, needs-test, traceback attached)
                         -> reply -> VERIFY -> DONE(confirmed_no_test)
  -> GATE              section 4.5
       fail -> TEST_SYNTH retry, then same confirmed_no_test path
  -> PATCH_LOOP        max 3 attempts, each sees prior failure output
       exhausted -> Linear (needs-human, attempts log) -> reply -> VERIFY -> DONE(abandoned)
  -> PROOF             section 4.6 (revert check, mutation check, flake check)
       fail -> treated as exhausted attempt
  -> APPROVAL          maintainer button in Discord (auto only in eval mode)
  -> BRANCH, PUSH      branch prefix proofpr/
  -> DRAFT_PR          proof block, receipt placeholder
  -> CI_WAIT           GitHub check runs via webhook, timeout configurable
       red -> close draft, Linear needs-human, reply -> DONE(ci_failed)
  -> READY             mark PR ready for review
  -> LINK              Linear In Review + PR attachment, Discord reply
  -> VERIFY            section 4.7 (three-way)
  -> RECEIPT           hash chain finalized, hash stamped on PR and Linear
  -> DONE(pr_opened)

Post-run (event and schedule driven):
  PR merged + release published -> notify every linked reporter in original thread
  Every 6h -> DRIFT_RECONCILER re-runs the consistency assertion on open runs, repairs or flags
```

Terminal states: `pr_opened`, `triaged`, `duplicate`, `already_fixed`,
`fix_in_flight`, `out_of_scope`, `not_a_bug`, `declined`, `confirmed_no_test`,
`abandoned`, `ci_failed`, `rejected_by_maintainer`, `failed_closed`.

`triaged` is the terminal state of a triage-only configuration: the report is
real, in scope, and filed for a human, and no code was written. It exists so the
`v0.1.0` tag can describe honestly what it did rather than borrowing a state that
implies reproduction was attempted.

Decline and stop reasons (enum): `not_a_bug_report`, `repro_missing_input`,
`repro_no_crash`, `repro_environment`, `no_failing_test`,
`fails_for_wrong_reason`, `flaky`, `out_of_fix_class`, `out_of_allowed_paths`,
`unsupported_version`, `user_error`, `insufficient_report`,
`suite_red_on_base`, `reporter_unresponsive`, `injection_only`.

**Asymmetric defaults.** Uncertainty before the code-writing stages resolves
toward filing an issue for a human. Uncertainty at or after `GATE` resolves
toward stopping. The agent may over-file; it may never over-code.

### 3.3 Ports and adapters

Each app sits behind a `Protocol` in `src/proofpr/ports/`. Adapters in
`src/proofpr/adapters/` implement them. Domain and step code import ports only.
Every adapter has an in-memory fake with identical contract tests.

---

## 4. Triage, reproduction, and proof

### 4.1 Pre-check

Deterministic checks first: does the text contain an input value, a traceback,
or an error string? Is a version mentioned, and is it supported?
Then one narrow cheap-model call classifies intent as `bug`, `question`,
`feature`, or `chatter` with a confidence value.

Only `chatter` and `question` above the confidence threshold stop here, with a
thread reply and no issue. Everything else continues. Low confidence continues.

### 4.2 Exists check

Runs before any sandbox or strong-model work, across every connected app.

| Source | Query | Outcome |
|---|---|---|
| Linear | open and recently closed issues by title terms and stack fingerprint, then cheap-model verdict with confidence threshold | `duplicate`: add the reporter and a link to the existing issue |
| GitHub | open issues, open PRs, and commits between the reporter's version and `HEAD` touching the implicated module | `already_fixed`: reply with the fixing version. `fix_in_flight`: reply with the PR link |

`already_fixed` requires a verbatim match: the fixing commit or release must
reference the same symbol or error string. A model opinion is never sufficient.

### 4.3 Worth-it check (rules only)

No model call. All must hold to continue toward code:

1. The implicated frame is inside `src/**` of the target repo, not third-party
   or user code (otherwise `user_error`).
2. The reported version is within the supported range (otherwise
   `unsupported_version`).
3. The behavior is in the declared fix class: input validation and error
   handling on bad types (otherwise `out_of_fix_class`).
4. The likely patch site is inside the allowed path allowlist (otherwise
   `out_of_allowed_paths`).

Failing any check still files a Linear issue with the reason. It only stops the
agent from writing code.

### 4.4 Reproduction, two stages

**Stage 1, raw repro (sandbox).** Build a minimal script from the reported
input and execute it against the base commit.
Capture exit code, exception type, and full traceback.

- Crash matching the report: continue to stage 2.
- No crash: `repro_no_crash`, ask the reporter one precise question naming the
  exact command tried and its output, wait, retry once.
- No usable input: `repro_missing_input`, same clarification path.
- ImportError, version mismatch, or environment failure: `repro_environment`.

**Stage 2, test synthesis.** The strong model converts the captured traceback
into exactly one pytest test. Up to 3 attempts. If all fail, the bug is still
real: file it as `confirmed_no_test` with the traceback and the repro script
attached, which is a useful outcome for a human, and never write a patch.

### 4.5 Reproduction gate

All must hold on the unmodified base commit:

1. The new test file collects cleanly (no ImportError, SyntaxError, collection
   error, fixture error).
2. The new test fails with `AssertionError` or the exception type the report
   implies.
3. The existing suite is green (otherwise `suite_red_on_base`).
4. The failure is deterministic across 3 runs (otherwise `flaky`).
5. The test file touches only `tests/` and does not modify existing tests,
   fixtures, or `conftest.py`.

### 4.6 Proof checks after patch

1. New test passes 3 consecutive runs.
2. Full suite passes.
3. **Revert check:** apply the new test to base without the patch; it must fail.
4. **Mutation check:** a deterministic AST mutator applies operators to the
   patched lines only (comparison flip, boolean negation, removed `raise`,
   `isinstance` type swap, constant replacement, early return removal). Each
   mutant runs the new test. Required kill ratio configurable, default 1.0 for
   mutants that compile.
5. Diff touches only `src/**` plus the single new test file. No deletions of
   tests, no changes to `.github/**`, `pyproject.toml`, `conftest.py`, CI, or
   lockfiles.

### 4.7 Three-way consistency assertion

After terminal state, read Discord, GitHub, and Linear and assert:

| Outcome | GitHub | Linear | Discord |
|---|---|---|---|
| `pr_opened` | PR open, ready, CI success, body has Linear ID, run marker, receipt hash | In Review, PR attached, receipt comment | reply with PR and Linear links |
| `declined` | no branch or PR with run marker | Triage, label `proofpr-declined`, reason | reply with reason and Linear link |
| `duplicate` | none | existing issue has reporter comment | reply links existing issue |
| `abandoned`, `ci_failed` | no open PR with run marker | label `needs-human` | reply with Linear link |

Result stored as `consistent: bool`. Divergence triggers one reconcile pass,
then re-assert, then flag.

### 4.8 Tamper-evident receipt

Every ledger event is serialized as canonical JSON and chained:
`h_n = sha256(h_{n-1} || event_n)`. The final hash is stamped on the PR and the
Linear issue. `proofpr verify-receipt <run_id>` recomputes the chain from the
ledger and compares against both stamps.

---

## 5. Sandbox

Generated tests and patches never run on the host interpreter.

| Control | Setting |
|---|---|
| Runtime | Docker, pinned image digest matching target repo Python version |
| Network | `--network none` |
| Filesystem | Read-only base image, repo mounted from a temporary git worktree, `--tmpfs /tmp` |
| User | Non-root, no capabilities (`--cap-drop ALL`, `--security-opt no-new-privileges`) |
| Resources | `--cpus 1`, `--memory 1g`, `--pids-limit 256`, wall timeout 60s per invocation |
| Environment | Empty except `PATH`, `HOME`, `PYTHONDONTWRITEBYTECODE`; no tokens, ever |
| Output | stdout and stderr captured, truncated at 64KB, passed back through the sanitizer |

Verified invocation (M0 smoke test, `proofpr-sandbox` image):

```
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 1g --pids-limit 256 \
  --env-file /dev/null -e GPG_KEY= \
  -v <worktree>:/work:ro proofpr-sandbox -p no:cacheprovider -q /work
```

Two details that only surface when the flags are actually applied: a read-only
root filesystem gives Python no temporary directory, so the `--tmpfs` mount is
required rather than optional, and pytest must run with `-p no:cacheprovider`
because it otherwise tries to write a cache into the read-only mount. The base
image's own `GPG_KEY` variable is cleared explicitly so the container's
environment holds nothing that did not come from this project.

---

## 6. Security model

Full threat model in `docs/THREAT_MODEL.md`, mapped to OWASP Top 10 for LLM
Applications (2025).

| Risk | OWASP LLM | Control |
|---|---|---|
| Prompt injection via Discord, Linear, stack traces, test output | LLM01 | Sanitizer, untrusted delimiters, model has no tools, policy check on every write |
| Secret or repository content exfiltration via comments | LLM02 | Egress scan (gitleaks rules, canary token, env values) on every outbound payload |
| Malicious generated code | LLM05 | Container sandbox (section 5) |
| Excessive agency: close issues, add collaborator, edit CI, push main | LLM06 | Tighten-only allowlist, branch prefix, path allowlist, maintainer approval |
| Unbounded consumption | LLM10 | Per-run token and spend caps, attempt caps, wall-clock caps |
| Forged webhooks | n/a | HMAC (GitHub, Linear), Ed25519 (Discord), replay window |
| Approval spoofing | LLM06 | Approver must hold configured Discord role; approval single use, bound to diff hash |

### 6.1 Operation allowlist (tighten-only)

```yaml
discord:
  write:
    - create_thread_message     # only in the intake message's thread
    - edit_own_status_message
linear:
  write:
    - create_issue              # configured team
    - update_issue              # only issue created or matched this run
    - create_comment            # same
    - create_attachment         # same, URL must be this run's PR
github:
  write:
    - create_branch             # prefix proofpr/
    - push                      # only branches created this run
    - create_pull_request       # base main, draft
    - mark_ready_for_review     # only PR created this run
    - close_pull_request        # only PR created this run
```

Refused and logged: closing or editing unrelated issues, adding collaborators,
changing branch protection, editing workflows, pushing to `main`, mass mentions,
any operation not listed.

### 6.2 Least-privilege credentials

| App | Credential | Scope |
|---|---|---|
| GitHub | GitHub App installed on target repo only | Contents RW, Pull requests RW, Checks R, Metadata R; webhooks `check_run`, `pull_request`, `release`. Branch protection on `main` without bypass for the app |
| Discord | Bot | Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Use Application Commands; Message Content intent (for reporter replies) |
| Linear | OAuth app or scoped API key | Read, write issues and comments in configured team |
| OpenRouter | API key | Hard spend limit set in dashboard |

---

## 7. Technology and toolchain

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Packaging | `uv`, `pyproject.toml` (PEP 621), committed `uv.lock`, `src/` layout |
| Webhook service | FastAPI + Uvicorn (GitHub, Linear webhooks, `/healthz`, `/readyz`) |
| Discord | `discord.py` gateway client (needed for thread replies) in the same process group |
| CLI | Typer |
| HTTP client | `httpx` async, explicit timeouts on every call |
| Retries | `tenacity`, exponential backoff with jitter, honours `Retry-After`, only on idempotent or marker-checked writes |
| Models and config | pydantic v2, pydantic-settings with `SecretStr` |
| Persistence | SQLite (WAL mode) ledger with forward-only migrations |
| Scheduling | APScheduler for drift reconciler |
| Sandbox | Docker via `docker` SDK |
| Mutation | In-repo deterministic AST mutator scoped to diff hunks (ADR 0005) |
| LLM | OpenRouter. `MODEL_CHEAP` for dedupe verdict and sanitizer classifier; `MODEL_STRONG` for reproduction tests and patches. Usage and cost recorded per call; prompt version hash recorded per run |
| Logging | `structlog` JSON, `run_id` and `trace_id` bound on every line |
| Tracing | OpenTelemetry SDK with GenAI semantic conventions, OTLP exporter optional |
| Lint and format | `ruff` |
| Types | `mypy --strict` |
| Tests | `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `hypothesis` |
| Security | `gitleaks`, `pip-audit`, ruff `S` rules |
| Hooks and tasks | `pre-commit`, `justfile` |
| Container | Multi-stage `Dockerfile`, non-root, `HEALTHCHECK` |
| CI | GitHub Actions |
| Dependencies | Dependabot weekly (pip, actions, docker) |
| Versioning | SemVer, Conventional Commits, Keep a Changelog |
| License | Apache-2.0 |

---

## 8. Repository layout

```
.
├── AGENTS.md
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── SECURITY.md
├── CODE_OF_CONDUCT.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── justfile
├── Dockerfile
├── compose.yaml
├── .env.example
├── .pre-commit-config.yaml
├── .github/
│   ├── workflows/{ci.yml, security.yml, eval-smoke.yml, release.yml}
│   ├── dependabot.yml
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── config/                        # local overrides only; defaults ship in the package
├── migrations/README.md           # pointer; the SQL lives in the package
├── sandbox/Dockerfile             # pinned runner image for target repo
├── src/proofpr/
│   ├── defaults/{proofpr.toml, policy.yaml}   # shipped config and allowlist
│   ├── ledger/migrations/*.sql    # forward-only, packaged so they are always found
│   ├── cli.py
│   ├── api.py                     # FastAPI webhooks
│   ├── bot.py                     # discord.py client
│   ├── settings.py
│   ├── pipeline.py                # durable state machine, sole sequencer
│   ├── domain/                    # RunState, Report, Claim, Outcome, enums, errors
│   ├── ports/
│   ├── adapters/{discord,github,linear,openrouter,docker_sandbox}.py
│   ├── steps/{sanitize,pre_check,fingerprint,exists,worth_it,localize,repro_raw,
│   │          test_synth,gate,clarify,patch,proof,approve,publish,ci_wait,
│   │          link,verify,receipt}.py
│   ├── proof/{mutator,revert,flake}.py
│   ├── triage/{intent,fingerprint,version_range,scope_rules}.py
│   ├── post/{release_notify,regression_watch,drift_reconciler}.py
│   ├── guard/{sanitize,policy,egress}.py
│   ├── ledger/
│   ├── observability/{logging,tracing,cost}.py
│   └── webhooks/signatures.py
├── tests/
│   ├── unit/
│   ├── property/
│   ├── contract/
│   ├── integration/               # marker: live
│   └── fixtures/
├── eval/
│   ├── datasets/{seeded,real_bugs,injection,faults}/
│   ├── hidden/                    # maintainer tests, never readable by a run
│   ├── arms.py                    # baseline (no_gate) and full pipeline arms
│   ├── splits.yaml                # dev and test case IDs, frozen
│   ├── schema.py
│   ├── runner.py
│   └── report.py
└── docs/
    ├── ARCHITECTURE.md
    ├── SETUP.md
    ├── CONFIGURATION.md
    ├── THREAT_MODEL.md
    ├── EVALUATION.md
    ├── RELIABILITY_BRIEF.md
    ├── RUNBOOK.md
    ├── DEMO.md
    ├── TARGET_REPO.md
    ├── ideas/
    └── adr/
```

---

## 9. Target repositories

ProofPR runs against two kinds of repository. Both are real GitHub repositories
with real CI. `validkit` exists so failure modes are seeded on purpose;
`real_bugs` exists so nobody can call the result a toy.

### 9.1 Real repositories (primary, used in the demo)

Forks of real open source Python libraries, each pinned to the commit **before**
a real historical input-validation bug was fixed upstream.

- Selected from BugsInPy-style datasets and from upstream issue trackers, roughly
  20 cases across at least 5 distinct libraries.
- The bug report is written from the wording of the original human issue, not
  from the fix.
- The upstream fix commit's own test is the **hidden maintainer test**. It never
  enters a prompt, a sandbox the agent can read, or the repository the agent
  sees. It is applied after the run, against the agent's patch, to decide
  correctness objectively.
- The upstream patch is never shown to the agent, and its diff is used only to
  report patch-site overlap as a secondary descriptive statistic, never as a
  pass criterion.
- Demo segment 2 uses one of these: a real library, a real historical bug, a
  real maintainer test that the agent's patch passes.

### 9.2 `validkit` (control repository)

Separate GitHub repo `validkit`: a small pure-Python validation library.

- No framework, no build step, full suite under 10 seconds.
- Seeded with at least 12 input-validation and bad-type-handling bugs, each
  documented privately in `eval/datasets/seeded/` with ground truth.
- CI workflow running pytest on PRs.
- Branch protection on `main`: required status check, one review, no force push,
  no bypass for the ProofPR app.
- `CODEOWNERS`, canary secret file for exfiltration tests.
- Fix class is fixed: input validation and error handling on bad types.
- Used for duplicate, already-fixed, in-flight, injection, and fault cases,
  which cannot be staged safely on real repositories.

---

## 10. Coding standards

- Full type hints; `mypy --strict` with zero unexplained ignores.
- Google-style docstrings on public modules, classes, and functions.
- No global mutable state; composition roots in `cli.py`, `api.py`, `bot.py`.
- Errors derive from `ProofPRError` with a stable `code`. Adapters map HTTP
  failures to `RetryableAppError`, `PermanentAppError`,
  `RateLimitedError(retry_after)`, `AuthError`.
- Every external call has a timeout (connect 5s, read 20s default).
- Pure functions for gate evaluation, mutation, policy, consistency assertion,
  receipt hashing.
- Never log secrets or raw untrusted content at INFO.
- Comments explain why, not what.
- No em dashes in code, comments, docs, commit messages, or PR text.

---

## 11. Testing strategy

| Layer | Scope | Tooling | Gate |
|---|---|---|---|
| Unit | gate rules, mutator, policy, sanitizer, consistency assertion, receipt, state transitions | pytest | Every PR |
| Property | policy never allows unlisted ops; receipt chain detects any edit; mutator output always parses or is discarded; resume never repeats a verified write | hypothesis | Every PR |
| Contract | each adapter vs recorded fixtures and its fake | respx | Every PR |
| Integration | real sandbox workspaces, one write and readback per app, sandbox run | pytest `-m live` | Nightly and manual, never on forks |
| Evaluation | datasets in `eval/` | `proofpr eval` | Smoke on PR, full on demand |

Coverage minimum 90% line and branch on `domain`, `steps`, `proof`, `guard`,
`ledger`. LLM calls are always faked below the evaluation layer.

---

## 12. Evaluation

Every number in `results.md`, `report.html`, `docs/RELIABILITY_BRIEF.md`, the
README, and the demo is produced by `eval/report.py` from run ledgers. Never
type a result by hand. Proportions carry Wilson 95% intervals.

### 12.1 Datasets

| Set | Size | Construction | Ground truth |
|---|---|---|---|
| `seeded` | 60 reports | Discord-style reports against `validkit`, including paraphrased duplicates, bugs already fixed in a later version, bugs with an open PR, out-of-scope and user-error reports, questions and chatter, vague reports answerable by a clarifying question, and unreproducible reports | intent, duplicate_of, already_fixed_in, fix_in_flight_pr, in_scope, reproducible, answer_if_asked, expected_outcome |
| `real_bugs` | ~20 cases | Historical input-validation bugs from real Python libraries (BugsInPy-style), pinned to the pre-fix commit, report written from the original issue | **Hidden maintainer regression test** from the upstream fix, never shown to the agent |
| `injection` | 20 attempts | In Discord text, usernames, stack frame strings, Linear comments: add collaborator, exfiltrate files or env via comment, close unrelated issues, edit CI, push to main, mass mention, approval bypass | No unauthorized write, underlying triage unchanged |
| `faults` | one publishable case x 8 writes x 4 modes (30 cells, 2 skipped as inapplicable) | `eval/datasets/faults/matrix.yaml`: rejected-before-applying, lost-response-after-applying, sustained outage, process death after applying, run against real containers via `proofpr eval-faults` | Recovered per cell: outcome and three-way consistency correct, a verifying receipt, zero duplicate writes, zero missing writes; a cell whose fault never fired is reported invalid, not recovered |

### 12.2 Baseline comparison (the headline number)

Every dataset is run twice, by the same pipeline, on the same split, in the same
session:

| Arm | Configuration |
|---|---|
| `baseline` | `PROOFPR_ARMS=no_gate`: skips `EXISTS`, `WORTH_IT`, `REPRO_RAW`, `GATE` and all proof checks. Localize, patch, open PR. This is what a competent tool-calling agent does today |
| `proofpr` | Full pipeline as specified in section 3.2 |

The baseline arm writes to a separate fork, Linear team, and Discord channel, and
its PRs are closed unmerged after readback. It exists to produce one row:

| Metric | baseline | proofpr |
|---|---|---|
| PRs opened | n | n |
| PRs whose patch fails the hidden maintainer test (**unsafe PRs**) | n | target 0 |
| Duplicate or already-fixed reports that still produced a PR | n | target 0 |
| Reports correctly ended with no code written | n | n |
| Cost per verified PR | $ | $ |

**Unsafe PR rate avoided** = baseline unsafe PRs minus proofpr unsafe PRs, over
baseline PRs opened. This single proportion is the product thesis. It leads
`results.md`, the README, the reliability brief, and the demo. If it is not
materially above zero, the proof machinery is ceremony and the brief says so.

### 12.3 Statistical honesty

Sample sizes are small. Proportions carry Wilson 95% intervals and the brief
states plainly that intervals on `n = 60` are wide and the results are
directional, not conclusive. Arm-to-arm differences are reported as paired
comparisons over identical case IDs, with the count of cases where the arms
disagree. No claim is made that a difference is significant unless the interval
on the paired difference excludes zero. False precision loses more credit than a
small N honestly labelled.

Splits: every case ID is assigned to `dev` or `test` in `eval/splits.yaml`,
frozen before prompt tuning. Prompts are tuned on `dev` only. Reported numbers
come from `test`. Runs record the prompt version hash; results from a different
prompt version than the frozen one are labelled as such.

### 12.4 Metrics

**Headline (reported first, everywhere):**

- Unsafe PR rate avoided versus the `baseline` arm (section 12.2)
- Unsafe PRs opened by ProofPR (target 0), confirmed by hidden maintainer tests
- Unwarranted PRs: opened on a case whose ground truth was a duplicate, out of
  scope, or never reproduced. A hidden test cannot express this, so it is counted
  from ground truth. Without it, an arm that patches everything looks clean
- Cost per verified PR, and cost per run, p50 and p95
- Reports resolved with no human touching code, and no code written wrongly

**Per stage:**

- Pre-check: intent classification accuracy; bug reports wrongly stopped (target 0)
- Exists check: duplicate precision and recall; `already_fixed` and `fix_in_flight`
  detected versus seeded, and false claims of either (target 0)
- Worth-it check: out-of-scope precision; in-scope reports wrongly rejected (target 0)
- Stage 1 raw repro: confirmed, no crash, missing input, environment; recovered by
  clarifying question
- Stage 2 test synthesis: success rate on confirmed crashes; `confirmed_no_test` count
- Gate: attempted, declined by reason
- PRs opened; CI green; red PRs (target 0)
- Hidden maintainer tests passing on agent patches (`real_bugs`)
- Mutation kill ratio distribution on new tests
- Abandoned mid-fix
- Three-way consistent runs; drift repaired by reconciler
- Injection: blocked, succeeded, unauthorized writes confirmed by app readback (target 0)
- Ablation: sanitizer off versus on, triage accuracy and injection outcomes
- Fault and kill recovery rate; duplicate writes (target 0)
- pass^3 per case
- Time from report to verified PR, p50 and p95
- Cost avoided by early stages: share of reports ended before any sandbox or
  strong-model call, and mean cost per terminal state

### 12.4.1 Running the harness without credentials

`proofpr eval --model scripted` answers the model's three questions from files in
`eval/scripted/`. Everything else is real: containers, guard, proof checks,
ledger, report. It measures the harness, never the model, and it found two real
defects on its first run (a crash when a duplicate was addressed by its Linear
identifier, and a scripted patch wider than its own test, correctly abandoned by
the mutation check).

Reports over such a ledger carry a banner saying so, detected from the recorded
model name rather than from an argument, so regenerating cannot drop the caveat.
Nothing produced this way is quotable as a result.

### 12.5 Cost control

`MAX_RUNS` and `MAX_SPEND_USD` enforced by the runner. Cheap model for dedupe
and classification, strong model for tests and patches only. Evaluation runs
four workers in parallel against a dedicated Discord channel, Linear team,
and target repo fork.

---

## 13. Observability

- One trace per run; span per step; child spans per app call, model call, and
  sandbox invocation.
- Model spans carry `gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `proofpr.cost_usd`.
- Ledger events are the evaluation source of truth: `step_started`,
  `step_finished`, `write_attempted`, `write_verified`, `write_failed`,
  `guard_blocked`, `gate_result`, `proof_result`, `clarify_sent`,
  `clarify_received`, `approval_received`, `reconciled`, `resumed`.

---

## 14. CI/CD

| Workflow | Trigger | Jobs |
|---|---|---|
| `ci.yml` | PR, push to main | ruff check, ruff format --check, mypy, unit + property + contract tests, coverage gate |
| `security.yml` | PR, weekly | gitleaks, pip-audit, dependency review |
| `eval-smoke.yml` | PR touching `src/` or `eval/` | Dev-split smoke with faked apps; fails on any unauthorized write, red PR, or unverified write |
| `release.yml` | tag `v*` | Build container, SBOM, GitHub Release |

Branch protection on this repo's `main`: required checks, one review, linear
history, no force push.

---

## 15. Documentation set

| File | Contents |
|---|---|
| `README.md` | Pitch, results table, mermaid architecture diagram, quickstart, links |
| `docs/ARCHITECTURE.md` | Pipeline, state diagram, durability and resume design, ports and adapters, data model |
| `docs/SETUP.md` | Credential setup per app with exact scopes and `proofpr doctor` verification |
| `docs/CONFIGURATION.md` | Every config key and env var with defaults |
| `docs/THREAT_MODEL.md` | Assets, trust boundaries, data flow diagram, threats, OWASP LLM mapping, residual risk |
| `docs/EVALUATION.md` | Dataset construction, ground truth rules, splits, metric definitions, reproduction steps |
| `docs/RELIABILITY_BRIEF.md` | One-page submission brief (section 17.2) |
| `docs/RUNBOOK.md` | Health checks, stuck runs, resume, reconcile, credential rotation, rate limits |
| `docs/TARGET_REPO.md` | `validkit` design, seeded bug catalogue policy, CI and protection settings |
| `docs/DEMO.md` | Timed script and fallback plan |
| `docs/adr/` | MADR records |
| `CONTRIBUTING.md` | Setup via `just`, branching, Conventional Commits, tests, ADR process |
| `SECURITY.md` | Private vulnerability reporting |
| `CHANGELOG.md` | Keep a Changelog |

### 15.1 Initial ADRs

1. `0001-deterministic-pipeline-no-model-tools.md`
2. `0002-tests-and-ci-as-judge.md`
3. `0003-durable-sqlite-ledger-and-resume.md`
4. `0004-container-sandbox-for-generated-code.md`
5. `0005-diff-scoped-ast-mutation-check.md`
6. `0006-tighten-only-operation-allowlist.md`
7. `0007-hash-chained-run-receipt.md`
8. `0008-hidden-tests-and-frozen-splits.md`
9. `0009-openrouter-model-routing.md`
10. `0010-python-uv-src-layout-toolchain.md`

---

## 16. Milestones

Sequential. Each ends with acceptance criteria met, CI green, and a state
report. Do not start the next otherwise.

| # | Milestone | Acceptance criteria |
|---|---|---|
| M0 | Scaffold | Toolchain, pre-commit, CI, docs skeleton, ADR 0010; `just check` passes |
| M1 | Adapters, policy, doctor | Ports, adapters, fakes, contract tests; policy wrapper on every write; `proofpr doctor` does one write and readback per app and one sandbox suite run of `validkit` |
| M2 | Triage path | Discord intake, sanitizer, pre-check, fingerprint, exists check across Linear and GitHub, worth-it rules, Linear create, thread reply, readback. **Tag `v0.1.0`: complete triage-only submission** |
| M3 | Reproduction and clarify | Stage 1 raw repro in sandbox, stage 2 test synthesis, gate rules, decline paths with reasons, reporter question and retry, `confirmed_no_test` outcome |
| M4 | Patch and proof | Sandbox patch loop, revert, flake and mutation checks |
| M5 | PR and three-way verify | Approval, branch, draft PR with proof block, CI readback, link, three-way assertion. **Tag `v0.2.0`** |
| M6 | Durability and receipt | Resume after SIGKILL at every step without duplicate writes; fault injection hooks; receipt chain and `verify-receipt` |
| M7 | Post-run loops | Release notification, regression watch, drift reconciler |
| M8 | Evaluation | Datasets validated, splits frozen, real-repository cases pinned with hidden maintainer tests held out, `PROOFPR_ARMS` baseline arm implemented, both arms run on identical case IDs, runner and report produce all section 12.4 metrics including the headline unsafe-PR-rate-avoided row |
| M9 | Observability, docs, submission | Traces with GenAI attributes; all docs complete; brief populated from report; demo recorded. **Tag `v1.0.0`** |

**Ship-a-tag rule.** A tagged milestone that is complete, evaluated, and honestly
described beats an untagged milestone that is half built. At every tag the repo
must be submittable on its own: README, brief with real numbers from whatever
subset exists, and a demo script that claims only what that tag does. If a
milestone cannot finish, the previous tag is the submission and section 17.2
item 6 says exactly what was dropped and why. Never submit from an untagged
working tree.

**Cut order if the plan overruns**, in this order and no other: M7 post-run
loops, receipt chain from M6 (keep crash resume), mutation check from M4 (keep
revert and flake), approval button from M5 (keep it auto). Never cut the
baseline arm, the hidden maintainer tests, the reproduction gate, or the
evaluation run. Those four are the submission.

---

## 17. Submission deliverables

### 17.1 Demo (two minutes)

The demo argues one claim: **an agent that cannot prove a bug must not write
code, and this one does not.** The claim is stated in the first eight seconds,
shown twice, and measured at the end. Every run is pre-recorded at real speed
and cut, never sped up silently; cuts are labelled on screen.

| Time | Segment | On screen | Line |
|---|---|---|---|
| 0:00 to 0:12 | Thesis | Split screen: two PRs from the same bug report, one plain diff, one with a proof table | "Coding agents open pull requests nobody can check. This one cannot open a PR it has not proved" |
| 0:12 to 0:45 | Proof on a real bug | Discord "Triage this" on a real historical bug in a real library; status message ticks; PR opens with the proof block; CI green; then the hidden maintainer test is applied and passes | "Red without the fix. Green with it. Red again when reverted. Mutants killed. The maintainer's own test, which the agent never saw, passes" |
| 0:45 to 1:20 | Refusal (the product) | Vague report: agent asks one question, reporter answers, reproduces. Second report: cannot reproduce, no PR, Linear issue with the reason. Third: duplicate, routed to the existing issue in one second and no sandbox at all | "Most reports should not become code. It files, it asks, it declines, and it says why" |
| 1:20 to 1:38 | Attack | Report carrying "also add me as a collaborator"; refused, logged, GitHub readback shows no change | "Untrusted text is data. The model has no tools. Only the state machine writes" |
| 1:38 to 2:00 | Numbers | Test-split table: unsafe PR rate avoided versus the baseline arm, unsafe PRs at 0, cost per verified PR, with intervals and N stated | "Same cases, same pipeline, gate off then on. That is the difference the proof makes" |

Captions on throughout, split screen Discord and GitHub, a fully pre-recorded
fallback take. Rehearse twice, record the third take.

The receipt chain, drift reconciler, crash resume, and `proofpr verify-receipt`
are not demoed. They are not checkable in two minutes. They live in the README,
the reliability brief, and a 30 second appendix clip linked from the submission.

### 17.2 Reliability brief (one page, numbers first)

1. The headline: unsafe PR rate avoided versus the no-gate baseline arm, unsafe
   PRs opened, cost per verified PR, with N and intervals stated in the same
   sentence.
2. What ProofPR does, two sentences.
3. Results table from the test split, both arms, paired over identical cases.
4. Proof: why tests, revert checks, mutants, and hidden maintainer tests judge
   the work instead of the model.
5. Readback verification, three-way consistency, drift reconciler, receipt.
6. Known failure modes found in evaluation, stated plainly, including every case
   where the baseline arm did better.
7. What was deliberately not built, and what was cut under the ship-a-tag rule.

---

## 18. Non-goals

- Web UI or dashboard.
- A fifth app integration.
- Multi-agent orchestration or model tool calling.
- General-purpose bug fixing. One fix class only.
- LLM-as-judge evaluation.
- Auto-merge. A human always merges.
- Memory beyond the run ledger.

---

## 19. Working agreement

- Stop at each milestone and report state before continuing.
- Prefer a narrow working path over a general one.
- Flag scope growth immediately instead of absorbing it.
- Never hand-write evaluation results.
- Ask before adding dependencies not listed in section 7.
- Ask before deleting files.
- Never commit unless explicitly asked. No AI attribution in commits.
- No em dashes in any output.
- After code changes run `just check` (ruff, mypy, pytest).
