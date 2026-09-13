# Hidden maintainer tests for real_bugs

Every other file in this directory holds one upstream maintainer's own
regression test, extracted from the real fix commit named in the matching
case's `fixed_commit` field.

**Nothing here but this README is committed.** `.gitignore` excludes every
`.py` file in this directory.

Rules, without exception:

1. A file here is never placed in a prompt.
2. A file here is never mounted into a sandbox the agent can read.
3. A file here is applied only after a run reaches a terminal state, against
   the agent's patch, to decide correctness.

If a hidden test ever reaches the agent, every `real_bugs` number produced
since is void, and the dataset is rebuilt from a different set of bugs.

Regenerate this directory's contents by re-running the extraction that
produced them (each case's `notes` field says which real test file and commit
it came from); nothing here is fetched automatically the way
`scripts/fetch_real_bugs.py` fetches the target repositories, because the
extraction for a few cases (see notes) trims a larger upstream test file down
to just the part this bug needs, which is a judgment call, not a mechanical
diff.
