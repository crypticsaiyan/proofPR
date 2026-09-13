# 0002. Tests and CI judge the work, never a model

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

Something must decide whether a bug was real and whether a patch fixed it. The
cheap answer is to ask a model. That answer is correlated with the model that
wrote the patch, cannot be audited by a maintainer, and cannot be reproduced.

## Decision drivers

- The verdict must be reproducible by a maintainer on their own machine
- The verdict must be independent of the model that produced the work
- Evaluation credibility depends on an objective ground truth

## Considered options

1. An LLM-as-judge scoring the patch
2. A human maintainer approving each run
3. A test that fails before the patch and passes after, plus the project CI, plus hidden upstream tests on real bugs

## Decision

Option 3. The reproduction gate, the revert check, the flake check, the mutation
check, and the project's own CI produce the verdict. For real historical bugs,
the upstream maintainer's own test, never shown to the agent, decides correctness
after the fact.

## Consequences

**Good:**

- Every claim in a pull request is a command a reviewer can rerun
- Correctness on the real-bug dataset is measured against tests written by humans who were not involved
- No circularity between author and judge

**Bad:**

- Bugs whose reproduction cannot be expressed as a test are out of reach, and are declined rather than guessed at
- Wall clock cost: several sandbox runs per candidate patch

**Neutral:**

- The `confirmed_no_test` outcome exists precisely because this rule is strict

## Rejected options and why

### LLM-as-judge

Unreproducible, correlated with the generator, and unfalsifiable to a reviewer.
It is excluded from the evaluation entirely, including as a secondary metric, so
no reported number depends on model opinion.

### Human approval as the only judge

Does not scale, and it measures reviewer patience rather than agent correctness.
An approval step exists, but it gates publication, not truth.
