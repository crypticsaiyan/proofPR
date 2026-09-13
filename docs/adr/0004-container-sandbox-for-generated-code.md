# 0004. Model-written code runs only in a locked-down container

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

Reproduction tests and patches are written by a model from attacker-influenced
text, and then executed. Executing them in the agent's own process would give
attacker-influenced code the agent's credentials.

## Decision drivers

- Generated code must never see a credential
- Generated code must not reach the network
- A hostile or looping test must not take the host down

## Considered options

1. A subprocess with a scrubbed environment
2. A separate virtual machine or a hosted sandbox service
3. A pinned Docker container with no network and no privileges

## Decision

Option 3. Containers run `--network none`, read-only root, `--cap-drop ALL`,
`--security-opt no-new-privileges`, `--cpus 1`, `--memory 1g`,
`--pids-limit 256`, a 60 second timeout, and an empty environment. Test
dependencies are baked into the image so nothing needs to be fetched at run
time.

## Consequences

**Good:**

- Credential exfiltration through generated code is structurally impossible, not merely unlikely
- Resource exhaustion is bounded and observable
- The runner image is pinned by digest, so sandbox results are reproducible

**Bad:**

- Docker is required on the host, and the application container mounts the daemon socket read-only, which is the deployment's most privileged surface
- Container start-up adds roughly a second per sandbox invocation

**Neutral:**

- The socket mount is recorded as an accepted residual risk in docs/THREAT_MODEL.md

## Rejected options and why

### Subprocess with a scrubbed environment

Shares the kernel namespace, the filesystem, and the network with the agent. A
scrubbed environment stops the laziest exfiltration and nothing else.

### Virtual machine or hosted sandbox

Stronger isolation, but it adds either boot latency or an external dependency
that would have to hold credentials of its own.
