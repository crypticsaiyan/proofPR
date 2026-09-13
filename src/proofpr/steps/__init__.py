"""One module per pipeline step.

A step is a pure function of run state plus injected ports. It returns the next
state and the ledger events it produced. Steps never call each other; only
:mod:`proofpr.pipeline` sequences them.
"""
