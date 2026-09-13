# Hidden maintainer tests

This directory holds the upstream fix tests for the `real_bugs` dataset.

**Nothing here is committed.** `.gitignore` excludes every file in this directory
except this README.

Rules, without exception:

1. A file here is never placed in a prompt.
2. A file here is never mounted into a sandbox the agent can read.
3. A file here is applied only after a run reaches a terminal state, against the
   agent's patch, to decide correctness.

If a hidden test ever reaches the agent, every `real_bugs` number produced since
is void, and the dataset is rebuilt from a different set of bugs.

Populate with `proofpr eval fetch-hidden` (M8).
