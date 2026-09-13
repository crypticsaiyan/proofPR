# Evaluation

> Status: M0 skeleton. Construction rules are fixed now so the datasets cannot be
> shaped to flatter the result later. Numbers arrive in M8.

## The rule

Every number in `results.md`, `report.html`,
[RELIABILITY_BRIEF.md](RELIABILITY_BRIEF.md), the README, and the demo is
produced by `eval/report.py` from run ledgers. No figure is typed by hand. No
model opinion enters any metric.

## Arms

Both arms are the same pipeline, run on the same case identifiers, in the same
session.

| Arm | Configuration |
|---|---|
| `no_gate` | Skips the exists check, worth-it rules, reproduction, the gate, and every proof check. This is what a competent tool-calling agent does today |
| `proofpr` | The full pipeline |

The baseline writes to a separate fork, Linear team, and Discord channel, and its
pull requests are closed unmerged after readback.

**Unsafe PR rate avoided** is the headline: baseline unsafe pull requests minus
ProofPR unsafe pull requests, over baseline pull requests opened. A pull request
is unsafe when its patch fails the hidden maintainer test. If this proportion is
not materially above zero, the proof machinery is ceremony, and the brief says so.

## Datasets

| Set | Purpose |
|---|---|
| `seeded` | Reports against `validkit`, with ground truth for intent, duplicates, already-fixed, in-flight fixes, scope, and reproducibility |
| `real_bugs` | Real historical bugs in real libraries, pinned to the commit before the upstream fix, judged by the upstream maintainer's own test |
| `injection` | Attacks embedded in report text, usernames, stack frames, and Linear comments |
| `faults` | One planned failure per write, across every write the publishable case makes, in four shapes (see below), run against real containers |

## Splits

`eval/splits.yaml` assigns every case to `dev` or `test`, frozen before any
prompt tuning. Prompts are tuned on `dev` only. Reported numbers come from
`test`. Each run records its prompt version hash; results from a different prompt
version than the frozen one are labelled as such.

## Hidden tests

`eval/datasets/<dataset>/hidden/` holds the maintainer test for each case that
should produce a patch. The ones we wrote for `seeded` and `injection` are
committed; the upstream fix tests for `real_bugs` are not. Neither is ever
mounted into a sandbox the agent can read or placed in a prompt. They are
applied after a run completes, against the agent's patch, to decide correctness
objectively.

A verdict reached after a run is recorded as a ledger **annotation**, not an
event. Events are hash-chained and sealed by the receipt published in the pull
request; appending a post-hoc judgement would break the receipt it accompanies.
Annotations sit beside the chain, carry the case identifier and the expected
outcome, and are what makes the comparison paired even for runs that wrote no
code.

## Running without credentials

`proofpr eval --model scripted` replaces the model with fixed answers from
`eval/scripted/`. The pipeline, the container sandbox, the guard, the proof
checks, the ledger, and the report generator all run for real; only the model
does not. It exists to exercise the harness, and it has already earned its keep:
the first scripted run found a crash on the duplicate path and a patch broader
than its own test.

Every report generated over such a ledger carries a banner saying the numbers
measure the harness rather than a model, detected from the recorded model name
so regenerating the report cannot drop the caveat. Nothing produced this way may
be quoted as a result.

## Statistical honesty

Proportions carry Wilson 95% intervals. Arm comparisons are paired over identical
case identifiers, and the count of cases where the arms disagree is reported.
No difference is called significant unless the interval on the paired difference
excludes zero. Sample sizes are small; the brief states N and interval width in
the same sentence as every headline figure. False precision loses more credit
than a small N honestly labelled.

## Unwarranted pull requests

A hidden test can say whether a patch is correct. It cannot say whether the work
should have happened at all: a duplicate, an out of scope report, or one that
never reproduced warrants no pull request even if the code that came out of it
is harmless. Those are counted separately, from ground truth, as unwarranted
pull requests, and reported beside the unsafe rate. Without that column an arm
that patches everything can look clean.

## Fault recovery

`eval/datasets/faults/matrix.yaml` names one publishable case and every write
the pipeline makes for it. Each cell in the matrix runs that same case once,
with a single planned failure on a single write, in one of four shapes:

- **rejected before applying** (a 429): retrying is always safe.
- **lost response after applying** (a timeout, but the write landed): retrying
  blindly would write it twice, so the write must be looked up first.
- **sustained outage** (ten consecutive failures): retries run out, the run
  pauses, and `proofpr resume` finishes it once the application recovers.
- **process death after applying**: the same lost-response problem, but the
  process itself is gone rather than merely the response, so recovery can only
  come from a later `resume`, never from a retry inside the same process.

`proofpr eval-faults` runs every cell against real containers (`--model
scripted` needs no credentials; the pipeline, sandbox, guard, and ledger are
still real). A cell counts as **recovered** only when the outcome and three-way
consistency are correct, the receipt verifies, and nothing was written twice or
went missing. A cell whose fault never actually fired is reported **invalid**,
never recovered: a write the run did not reach proves nothing about recovering
from a failure on it.

This is where the receipt anchoring fix (ADR 0007) was found: before it, a
lost response on the receipt comment caused a genuine duplicate, because the
comment's own text (the receipt digest) was recomputed fresh on every attempt
and so never matched itself on retry.

## Metric definitions

See AGENTS.md section 12.4 for the authoritative list. Definitions that could be
gamed are written down here as each metric lands.
