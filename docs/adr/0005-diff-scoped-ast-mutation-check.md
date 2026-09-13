# 0005. Mutation testing scoped to the diff, with an in-repo AST mutator

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

A test that fails before a patch and passes after can still be vacuous: it may
assert almost nothing, or it may pin an implementation detail. Passing is a weak
signal of a meaningful test.

## Decision drivers

- A new test must be shown to detect the bug it claims to cover
- The check must finish in seconds, not minutes
- It must be deterministic so the same patch yields the same proof

## Considered options

1. No mutation testing; rely on the red-green transition
2. A general mutation tool such as mutmut or cosmic-ray over the module
3. A small deterministic AST mutator applied only to lines the patch touched

## Decision

Option 3. The mutator applies a fixed, ordered operator set (comparison flip,
boundary shift, boolean negation, constant substitution, return removal) to the
patched lines only. Every mutant must be killed by the new test.

## Consequences

**Good:**

- Proves the test is coupled to the fix rather than to the module at large
- Runtime is proportional to the diff, so it stays inside the run budget
- Deterministic operator order makes the proof reproducible and auditable

**Bad:**

- A hand-written mutator covers fewer operators than a mature tool
- Mutation is not a completeness proof; unkilled behaviour outside the diff is not examined

**Neutral:**

- The revert check covers the complementary direction: the test must fail again once the fix is removed
- String literal mutation was removed after it produced an equivalent mutant on a
  docstring during M4. Because the proof requires every mutant to die, an
  operator that can generate unkillable mutants would block honest patches and
  make the kill ratio meaningless. An operator earns its place only when
  surviving it is genuinely evidence of a weak test

## Rejected options and why

### No mutation testing

The red-green transition alone is satisfied by a test that asserts the exception
type and nothing about the behaviour. This is the most common way an agent's test
looks convincing and proves nothing.

### A general mutation tool

Slow across a whole module, non-deterministic in ordering, and it reports on code
the patch never touched, which produces noise in the pull request rather than
proof.
