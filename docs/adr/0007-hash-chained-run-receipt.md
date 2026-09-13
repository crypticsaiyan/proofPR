# 0007. Hash-chained tamper-evident run receipt

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

A pull request claims that a test failed, then passed, that mutants died, and
that CI was green. A reviewer sees a rendered table. Nothing stops that table from
being edited after the fact, by a person or by a later automated write.

## Decision drivers

- A reviewer must be able to verify the claims were not altered after the run
- Verification must not require trusting the agent that produced them
- It must add negligible cost per run

## Considered options

1. Trust the rendered proof block
2. Sign receipts with an asymmetric key and publish the public key
3. Hash-chain the ledger events and publish the final digest

## Decision

Option 3. Each ledger event extends the chain as
`h_n = sha256(h_{n-1} || event_n)`. The digest is published in the pull
request body, on the Linear issue, and in the Discord thread.
`proofpr verify-receipt <run-id>` recomputes it.

**The published digest is a checkpoint, not literally the last event before
close.** The run decides its outcome once (recorded as `run_stopped`) and
freezes that event's own chain hash as the receipt right there. Every write
that follows publishing it (the receipt comment itself, the four-way
consistency check, the final state snapshot) still extends the same chain, just
after the checkpoint rather than before it. `verify_receipt` accepts a
published value that matches *any* event's hash in an intact chain, not only
the second-to-last one, because a value computed fresh at closing time cannot
also be the value already handed to a reviewer minutes earlier without the
chain still growing in between.

An earlier version computed the digest live (`head_hash` at the moment of
posting) and recomputed it again at seal time. Fault injection testing (M8)
caught the consequence directly: on a retry after a lost response, the
recomputed digest differed from the one already posted, so the retry did not
recognize its own earlier attempt and posted a second, differently-numbered
receipt comment. Freezing the checkpoint at `run_stopped` removed the drift and
the duplicate at the same time: the same fix serves both concerns.

## Consequences

**Good:**

- Any edit to a recorded step is detectable by recomputation
- Cheap: one hash per event
- The receipt is a short string that travels naturally in text bodies

**Bad:**

- Tamper evidence, not tamper proofing: an attacker who controls both the ledger and every published copy of the digest can rewrite both
- Not demonstrable inside a two minute video, so it is documented rather than demoed

**Neutral:**

- Signing can be layered on later without changing the chain format

## Rejected options and why

### Trusting the rendered block

The entire premise of the project is that unverifiable claims are worthless. A
proof table that cannot be checked is exactly such a claim.

### Asymmetric signatures

Stronger, and it introduces key management for a single-operator deployment. The
chain is the part that adds value here; signing is deferred.
