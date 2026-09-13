# Threat model

> Status: M0 skeleton. Threats and mitigations are stated now because they shape
> the code. Evidence columns are filled from evaluation output in M8.

## Assets

1. Application credentials: GitHub PAT, Linear key, Discord bot
   token, OpenRouter key.
2. Write access to the target repository, including its default branch.
3. The integrity of published proof: if the proof block can lie, the product has
   no value.
4. The ledger, which is the evidence of what happened.

## Trust boundaries

| Boundary | Direction | Control |
|---|---|---|
| Application text into a prompt | inbound | `guard/sanitize`: delimited untrusted blocks, rule and classifier injection flags, flag recorded and content still usable as data |
| Model output into an action | inbound | Pydantic schema validation. The model names no operation, target, or path that the state machine had not already chosen |
| Any outbound write | outbound | `guard/policy` allowlist, then `guard/egress` canary and credential scan |
| Model-written code into execution | inbound | Container sandbox: no network, read-only, all capabilities dropped, empty environment |
| Webhook into the pipeline | inbound | Signature verification per provider before the body is parsed |

## Threats

| ID | Threat | Mitigation | Evidence |
|---|---|---|---|
| T1 | Injected instructions in a report cause an unauthorized operation | The model has no tools; every operation comes from code and passes the allowlist | `injection` dataset, unauthorized writes confirmed by readback, target 0 |
| T2 | Generated test or patch exfiltrates a credential | Sandbox has no network and an empty environment; egress scan on every payload; planted canary | Canary never observed in any outbound payload |
| T3 | A patch is published without real proof | Gate, revert, flake, and mutation checks are preconditions of publication, all recorded in the ledger | Unsafe PR count against hidden maintainer tests, target 0 |
| T4 | Proof is altered after the fact | Hash-chained receipt published in three places; `proofpr verify-receipt` recomputes | Receipt verification in the runbook |
| T5 | A crash leaves duplicate issues, branches, or comments | Ledger-based resume plus run markers searched before every write | SIGKILL at every step, duplicate writes target 0 |
| T6 | Forged webhook triggers a run | Per-provider signature verification before parsing | Contract tests with tampered signatures |
| T7 | Runaway spend or an infinite patch loop | Hard caps in the runner, patch attempts capped, sandbox timeout | Cost per run, p50 and p95 |
| T8 | Over-scoped credential used beyond its purpose | Fine-grained tokens scoped to one repository, team, and project; branch protection as a second layer | Setup checklist and `doctor` |

## OWASP LLM Top 10 (2025) mapping

| Risk | How it is addressed here |
|---|---|
| LLM01 Prompt injection | No tools, allowlist, sanitizer boundary, and an injection dataset with a sanitizer-off ablation |
| LLM02 Sensitive information disclosure | `SecretStr`, empty sandbox environment, egress scan, canary |
| LLM03 Supply chain | Pinned sandbox image digest, committed lock file, Dependabot, `pip-audit`, CodeQL |
| LLM04 Data and model poisoning | Untrusted text is never training data; frozen splits; hidden tests |
| LLM05 Improper output handling | Every model output is pydantic-validated; generated code executes only in the sandbox |
| LLM06 Excessive agency | The central decision of this project: fixed state machine, tighten-only allowlist, asymmetric defaults |
| LLM07 System prompt leakage | Prompts contain no secrets, so leakage is not a credential event |
| LLM08 Vector and embedding weaknesses | No vector store in scope |
| LLM09 Misinformation | Claims are only published when proved by tests; `already_fixed` requires a verbatim match, never a model opinion |
| LLM10 Unbounded consumption | Spend and run caps, patch attempt cap, sandbox CPU, memory, PID, and time limits |

## Residual risks, accepted on purpose

1. **Docker socket mount.** The application container mounts the daemon socket
   read-only to launch sibling sandboxes. Host compromise via the daemon is not
   mitigated by this design; a dedicated runner host is the answer in production.
2. **Tamper evidence, not tamper proofing.** An attacker controlling both the
   ledger and every published copy of the digest can rewrite both.
3. **Small evaluation samples.** Intervals are wide and reported as such.
4. **Mutation scope.** Only patched lines are mutated, so behaviour outside the
   diff is not examined.
