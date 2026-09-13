# 0009. Two-tier model routing through OpenRouter

- Status: accepted
- Date: 2026-09-13
- Deciders: OWNER

## Context and problem statement

Most reports never need a strong model. Duplicate detection, intent
classification, and injection flagging are short classification tasks. Test
synthesis and patching are not. Using one strong model everywhere wastes the
evaluation budget on reports that should have ended in the first second.

## Decision drivers

- Cost per run must stay low enough to run every dataset repeatedly, in two arms
- Model choice must be swappable without code changes
- Usage and cost must be attributable per step

## Considered options

1. One strong model everywhere
2. Direct provider SDKs with per-provider code
3. OpenRouter with a cheap tier and a strong tier selected by step

## Decision

Option 3. `MODEL_CHEAP` serves intent, duplicate verdicts, and the sanitizer
classifier. `MODEL_STRONG` serves test synthesis and patching only. Every call
records token usage, cost, and the prompt version hash.

## Consequences

**Good:**

- Cost avoided by early pipeline stages becomes a measurable, reportable quantity
- Swapping either tier is an environment variable
- One OpenAI-compatible client covers every model

**Bad:**

- A dependency on a single routing provider, and its availability
- Cost figures are provider-reported rather than independently measured

**Neutral:**

- The adapter is small enough that pointing it at a provider directly is a contained change

## Rejected options and why

### One strong model everywhere

Multiplies evaluation cost several times over for no accuracy gain on
classification, and it hides the economic argument that early triage stages are
what make the system affordable.

### Direct provider SDKs

More code, more credentials, and a rewrite whenever a model is swapped.
