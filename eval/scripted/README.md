# Scripted answers

A stand-in for the model, used by `proofpr eval --model scripted`.

It exists to exercise the harness without credentials: the pipeline, the
sandbox, the guard, the ledger, and the report generator all run for real, and
only the model is replaced by fixed answers taken from this directory.

**Numbers produced this way measure the harness, not a model.** Every report
generated with it is labelled `scripted-stub` and carries a banner saying so.
Nothing here may be quoted as a result.

- `fixes/<module>.py` is the corrected source file the scripted model proposes.
- `tests/<module>.py` is the reproduction test it writes.

The module is chosen from the traceback in the case text, so a case pointing at
`src/validkit/numbers.py` gets the numbers answers.
