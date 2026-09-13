# 0008. Hidden maintainer tests and splits frozen before tuning

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

An evaluation an author tunes against is a training set. Reporting numbers from
it overstates performance, and it is the single most common flaw in agent
benchmarks.

## Decision drivers

- Reported numbers must come from cases never used for tuning
- Correctness must be judged by tests the agent could not have seen
- Small sample sizes must be stated honestly rather than dressed up

## Considered options

1. One dataset, report everything
2. A held-out split only
3. A frozen dev and test split, plus hidden upstream maintainer tests on real historical bugs

## Decision

Option 3. `eval/splits.yaml` assigns every case to `dev` or `test` before any
prompt tuning, and records the prompt version hash per run. Real-bug cases are
pinned to the commit before the upstream fix, and the upstream fix's own test is
kept in `eval/datasets/real_bugs/hidden/`, excluded from version control, never mounted into a
sandbox the agent can read, and applied only after a run completes.

## Consequences

**Good:**

- Reported numbers are honest, and the hidden tests make correctness objective
- Prompt version hashes make it visible when a result predates the frozen prompt
- Paired arm comparison over identical case identifiers removes case-mix effects

**Bad:**

- Fewer usable cases per reported number, so intervals are wide
- Building the hidden set requires careful sourcing so the fix never leaks into a prompt

**Neutral:**

- The reliability brief states N and interval width in the same sentence as every headline figure

## Rejected options and why

### One dataset

Guarantees overfitting, and there is no way to tell from the outside how much.

### A held-out split alone

Better, and still judged by tests the agent may have influenced. The hidden
upstream tests are what make correctness independent of this project entirely.
