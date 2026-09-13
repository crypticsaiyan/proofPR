# Security policy

## Reporting a vulnerability

Report privately through
[GitHub private vulnerability reporting](https://github.com/OWNER/proofpr/security/advisories/new).
Do not open a public issue.

Expect an acknowledgement within three working days and an assessment within ten.

## Scope

This project runs model-written code and acts on live credentials across four
applications. The following are in scope and treated as high severity:

- Any input that causes an operation absent from the operation allowlist to execute.
- Any escape from the sandbox described in AGENTS.md section 5.
- Any path by which a credential, an environment value, or the canary reaches an
  outbound payload.
- Any way to make the agent publish a patch that did not pass the reproduction
  gate and the proof checks.
- Any forged webhook that is accepted as genuine.

## Design assumptions

- Every piece of application-sourced text is untrusted, including usernames,
  issue titles, stack frame strings, and comments.
- The model has no tools. It cannot choose an operation, only return data that a
  deterministic state machine may act on.
- Credentials are least privilege and scoped to a single repository, team, and
  project. The sandbox receives none of them.

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full model, the OWASP
LLM Top 10 mapping, and the residual risks that are accepted on purpose.
