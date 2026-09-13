# Hidden tests for the seeded dataset

One file per case, named `<case-id>.py`. They judge whatever patch an arm
produced, and they are applied **after** the run, in a fresh worktree.

These are committed, unlike the `real_bugs` hidden tests, because we wrote them:
there is no upstream maintainer to borrow from for a sample library. The rule
that matters is unchanged and enforced by the runner, not by convention:

1. A hidden test is never written into a worktree the agent can read.
2. A hidden test never appears in a prompt.
3. A hidden test is applied only after a run reaches a terminal state.

A case with no hidden test is reported as **unjudged**, never as safe. Counting
an unjudged patch as correct is the exact flattery the unsafe-PR metric exists to
avoid.
