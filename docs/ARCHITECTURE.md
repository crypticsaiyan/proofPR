# Architecture

> Status: M0 skeleton. The pipeline contract below is authoritative and matches
> AGENTS.md section 3. Implementation notes are filled in per milestone.

## Shape

ProofPR is a durable state machine with ports and adapters around it.

- `pipeline.py` is the **only** place steps are sequenced. Nothing else decides
  what happens next.
- `steps/` holds one module per step. A step is a function of run state plus
  injected ports, returning the next state and the events it produced. Steps
  never call each other.
- `ports/` defines protocols. `adapters/` implements them. Steps depend on the
  protocol only, which is what lets the whole pipeline run against fakes with no
  credentials.
- `ledger/` persists append-only events in SQLite WAL mode. It is the source of
  truth for resume and for every evaluation number.
- `guard/` is the security boundary: sanitizer in, policy and egress scan out.

## Step order

See AGENTS.md section 3.2 for the authoritative diagram. The ordering principle:
cheapest and most deterministic checks first, so sandbox time and the strong
model are spent only on reports that survive everything before them.

Implemented as of M2:

| Step | What decides | Model involved |
|---|---|---|
| `sanitize` | Rules | No |
| `pre_check` | Rules first; one narrow intent call only when no rule fires | Cheap, sometimes |
| `fingerprint` | Exception, innermost project frame, and stated version from the report | No |
| `exists` | Structural matching; the cheap model breaks ties only | Cheap, sometimes |
| `worth_it` | Rules only, by design | Never |
| `repro_raw` | Running the reported input in a container | Never |
| `clarify` | Rules choose the question; a model phrases it when available | Cheap, optional |
| `test_synth` | The strong model writes a test; structural rules refuse bad ones | Strong |
| `gate` | Four conditions, all measured by running pytest | Never |
| `patch` | The strong model proposes; the allowlist and the suite decide | Strong |
| `proof` | Revert, flake, and diff-scoped mutation, all by running pytest | Never |
| `approval` | A maintainer, or a configured policy. Failure means refusal | Never |
| `publish` | Guarded writes only: branch, two commits, draft pull request | Never |
| `ci_wait` | The checks API. Absence of checks is never success | Never |
| `verify` | Reads Discord, GitHub, and Linear and compares them to the outcome | Never |

## Why three proof checks and not one

Red to green shows correlation. Each check closes a specific way that can lie:

| Check | The lie it catches |
|---|---|
| Revert | The test was passing for an unrelated reason and the patch is incidental |
| Flake | The test passes sometimes, which will waste a maintainer's time forever |
| Mutation | The test does not actually examine the fix, only its surroundings |

Mutation is scoped to the lines the patch changed, so the check is proportional to
the change and the report is about this fix rather than the module's general
coverage. Operators that generate equivalent mutants are excluded: since every
mutant must die, an operator producing unkillable ones would block honest patches
and make the kill ratio meaningless.

## Reproduction, in two stages

Stage 1 answers "does it actually crash, and with what" by executing the reported
input. Only two sources are used, all extracted deterministically: a fenced
block, or the call line under the reporter's own traceback frame. A model is never asked to supply the input, because a
model asked for "the code that crashed" will invent a plausible call that never
appeared in the report, and stage 1 exists precisely to find out whether the
reported input crashes.

Stage 2 gives the strong model the captured traceback and the real source of the
frame it landed in, and asks for one test. The result is checked structurally
before it runs (one test function, under `tests/`, parses, asserts something,
imports nothing exotic) and then checked behaviourally by the gate.

A step ends the run by raising `Stop`, carrying the state as of that step. Most
runs end this way, and ending early is the product working rather than failing.

## Where the model is, and is not

Two calls exist in the whole pipeline: intent classification and a duplicate
tie-break. Both return a small validated object. Neither names an operation, an
issue, a branch, or a path. The scope decision, which is the most subjective
question in the system, is deliberately rules-only, so that "is this worth
working on" has an auditable answer and a measurable false-reject rate.

## Durability and resume

Each step writes `step_started` before acting and `step_finished` after. Resume
replays the ledger, finds the last unfinished step, and re-enters it. Re-entry is
safe because every outbound write carries a `proofpr-run:<id>` marker that is
searched for before the write is attempted.

_To be completed in M3 and M6: state transition table, marker search semantics
per application, and the resume test matrix._

## Sandbox contract

The exact verified `docker run` invocation, including the two flags that are only
discoverable by running it (`--tmpfs /tmp` and pytest's `-p no:cacheprovider`
under a read-only root), is recorded in AGENTS.md section 5.

## The write path

Every write in the system takes the same route, and there is no other route:

```
step builds a WriteIntent(run_id, app, operation, target, payload)
  -> Guard.authorize
       Policy.check      allowlist, denied_always, per-operation constraints
       EgressScanner.scan canary, credential shapes, environment secret values
  -> adapter performs the call, body carrying `proofpr-run:<id>`
  -> Ledger.record_write  unverified
  -> adapter.readback      re-reads the application
       disagreement -> VerificationError, the step fails
  -> Ledger.record_write  verified
```

A `WriteIntent` is the unit the guard can reason about, which is why adapters
refuse to write without one. The marker in the body is what makes a retry after a
crash idempotent: the ledger is consulted first, and the remote application
second.

## After the run

Two loops, neither of which makes a new decision about code:

| Loop | Trigger | What it does |
|---|---|---|
| Release notification | GitHub merge webhook | Tells the reporter and comments on the issue |
| Drift reconciler | Every six hours, or `proofpr reconcile` | Re-runs the consistency assertion and repairs what it safely can |

The reconciler repairs by re-applying writes the run was already entitled to
make, through the same guard, carrying the same run marker. A divergence that
would need a new decision, such as a missing pull request, is reported and left
for a human. That boundary is the difference between a reconciler and an agent
that quietly does more work than anyone approved.

## Data model

- `WriteIntent`, `WriteResult`, `HealthCheck`, `UntrustedText` in
  `domain/models.py`, all frozen.
- `Step`, `Outcome`, `Reason`, `Arm` in `domain/enums.py`. These are persisted in
  the ledger and appear in evaluation output, so they are treated as a wire
  format.
- `UntrustedText` exists so that putting raw application text into a prompt is a
  type error rather than an oversight.

Ledger schema: `runs`, `events` (append-only, hash chained), `writes` (keyed by
run, operation and target for idempotent retry), `model_calls`. Migrations are
forward-only; see `migrations/README.md`.

## Related

- [ADR 0001](adr/0001-deterministic-pipeline-no-model-tools.md)
- [ADR 0003](adr/0003-durable-sqlite-ledger-and-resume.md)
