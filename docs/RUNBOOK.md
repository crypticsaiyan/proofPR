# Runbook

> Status: M0 skeleton. Procedures are added as the capability they operate on
> lands.

## Health

```bash
curl -fsS localhost:8080/healthz     # process is up
curl -fsS localhost:8080/readyz      # ledger writable, adapters reachable
just doctor                          # one real write and readback per app
```

## A run is stuck, or the process died

```bash
proofpr verify-receipt <run-id> --show-events   # what happened, and where it stopped
proofpr resume <run-id>                         # continue from the last finished step
proofpr resume                                  # continue every unfinished run
```

1. A run stuck inside a step has a `step_started` with no `step_finished`. That
   step is the one resume re-enters.
2. Re-entry writes nothing twice: every write is recorded in the ledger before it
   is attempted, and a write already verified is skipped rather than repeated.
3. Steps that finished are not run again, so a crash after reproduction does not
   pay for the sandbox twice.
4. A run whose very first step never finished cannot be resumed, and says so. Run
   it again from the report.
5. If the same step fails repeatedly, the run ends in `failed_closed` rather than
   retrying forever, and it is left for a human.

## Checking a receipt

```bash
proofpr verify-receipt <run-id>
```

The receipt covers every event up to and including the last write. It is
published in the pull request, on the Linear issue, and in the thread, so a
reviewer can compare what they were shown against what was recorded. An edited
event, a deleted event, a forged receipt, or an event appended after the run
closed all fail verification and say which.

## The applications disagree with the ledger

```bash
proofpr reconcile
```

Runs the same comparison the scheduled reconciler performs every six hours.
Repairs are themselves recorded as `reconciled` events and counted in the
evaluation.

## Rate limited

Adapters honour `Retry-After` and back off exponentially with jitter. Sustained
429s mean the cap should be lowered, not that retries should be increased.

## Credential rotation

Rotate in the provider, update `.env`, restart, run `just doctor`. A credential
change that `doctor` does not confirm has not taken effect.

## Suspected credential exposure

1. Revoke first, investigate second.
2. Check for the canary in the ledger's `write_attempted` payload hashes.
3. Rotate every credential for the affected application, not only the exposed
   one.
4. Record it in the security advisory flow described in `SECURITY.md`.
