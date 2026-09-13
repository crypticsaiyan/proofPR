# 0006. A tighten-only operation allowlist in version control

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

The blast radius of the agent is the set of operations it can perform. If that
set is implicit in the code, it drifts, and nobody can state it during review.

## Decision drivers

- The blast radius must be stated in one reviewable place
- Widening it must be deliberate and recorded
- Refusals must be counted, not merely logged

## Considered options

1. Rely on token scopes alone
2. An allowlist in code
3. A declarative allowlist file that may tighten at any time and widen only through an ADR and a CODEOWNERS review

## Decision

Option 3. `policy.yaml`, shipped in the package and overridable at
`config/policy.yaml`, enumerates every permitted operation with its
constraint, plus operations denied unconditionally. Anything absent is refused,
recorded as a `guard_blocked` event, and counted in the evaluation.

## Consequences

**Good:**

- The answer to what can this thing do is a single file a reviewer can read in a minute
- Refusal counts become a reported metric rather than an anecdote
- Token scopes remain as an independent second layer, along with branch protection

**Bad:**

- The policy file and the adapter code can drift, so contract tests assert every adapter operation appears in the policy

**Neutral:**

- Tightening needs no ceremony, which keeps the safe direction frictionless

## Rejected options and why

### Token scopes alone

Scopes are coarse. A token that may open a pull request may usually also comment
on arbitrary issues, and scopes cannot express only this run's branch.

### An allowlist in code

Better than nothing, but the rules end up spread across adapters and become a
diff-reading exercise rather than a statement.
