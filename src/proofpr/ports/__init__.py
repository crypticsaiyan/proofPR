"""Protocol definitions for every external application.

Steps depend on these protocols, never on a concrete adapter. Fakes in
``tests/fixtures`` implement the same protocols, which is what lets the whole
pipeline run without credentials.
"""
