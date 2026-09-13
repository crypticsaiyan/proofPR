"""ProofPR: Discord bug reports to proof-carrying pull requests, or an honest decline."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proofpr")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
