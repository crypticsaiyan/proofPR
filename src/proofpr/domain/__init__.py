"""Domain model: states, outcomes, reasons, and errors.

Nothing in this package performs IO or imports an adapter. It is the shared
vocabulary used by the pipeline, the steps, the ledger, and the evaluation
harness.
"""

from proofpr.domain.enums import Arm, Outcome, Reason, Step
from proofpr.domain.errors import (
    ConfigurationError,
    GuardBlockedError,
    PermanentAppError,
    ProofPRError,
    RetryableAppError,
    SandboxError,
    VerificationError,
)

__all__ = [
    "Arm",
    "ConfigurationError",
    "GuardBlockedError",
    "Outcome",
    "PermanentAppError",
    "ProofPRError",
    "Reason",
    "RetryableAppError",
    "SandboxError",
    "Step",
    "VerificationError",
]
