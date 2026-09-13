# 0001. Deterministic pipeline, the model gets no tools

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

The agent holds live credentials for four applications and acts on text written
by strangers. A tool-calling model decides, at inference time, which operation to
invoke. That makes the set of reachable operations a property of a prompt rather
than a property of the code, and it makes prompt injection an authorization
bypass instead of a content problem.

## Decision drivers

- Injected text must never be able to select an operation
- The reachable operation set must be reviewable by reading code, not prompts
- Runs must be reproducible and resumable after a crash
- Evaluation must attribute failures to a specific step

## Considered options

1. A tool-calling agent with a guarded tool layer
2. A planner model that emits a plan, executed by code
3. A fixed state machine in code; the model only returns validated JSON

## Decision

Option 3. `pipeline.py` is the sole sequencer. Every model call has a fixed
input contract and a pydantic output schema. The model never names an operation,
a repository, an issue, or a branch that the state machine did not already decide
to act on.

## Consequences

**Good:**

- Prompt injection degrades to a content-quality problem, never privilege escalation
- Each step is independently unit testable with the model mocked
- Durable resume is possible because the next step is a function of persisted state
- Costs and latency are predictable per step

**Bad:**

- No emergent behaviour; anything the pipeline does not model, it cannot do
- New capabilities need code, not a prompt change

**Neutral:**

- The architecture is closer to a workflow engine than to an agent framework, which is a deliberate reading of the word agent

## Rejected options and why

### A tool-calling agent with a guarded tool layer

A guard on the tool layer still lets the model choose among allowed operations
under adversarial influence, and the allowed set is large enough to do damage by
composition. Auditing becomes an argument about prompts.

### A planner model executing through code

Better, but the plan is still model-authored, so its shape is attacker
influenced. Verifying an arbitrary plan is harder than executing a fixed one.
