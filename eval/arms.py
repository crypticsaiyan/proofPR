"""The two arms, and what separates them.

Both arms are the same pipeline, the same guard, the same ledger, and the same
model. The difference is the checks, and the difference is the result this
project reports.

`no_gate` is not a strawman. It localizes from the traceback, writes a patch with
the strong model, keeps the project's suite green, and opens a pull request. That
is what a competent tool-calling agent does today, and everything it skips is
something a reasonable person could argue is unnecessary.
"""

from __future__ import annotations

from dataclasses import dataclass

from proofpr.domain.enums import Arm, Step

#: What the baseline never does. Each entry is a check whose absence the
#: evaluation is designed to price.
SKIPPED_BY_BASELINE: dict[Step, str] = {
    Step.EXISTS: "does not check whether the work already exists anywhere",
    Step.WORTH_IT: "does not check scope, version, or fix class",
    Step.REPRO_RAW: "never runs the reported input, so never learns what it does",
    Step.CLARIFY: "never asks the reporter anything",
    Step.TEST_SYNTH: "writes no reproduction test",
    Step.GATE: "has no gate to pass",
    Step.PROOF: "no revert check, no flake check, no mutants",
}


@dataclass(frozen=True, slots=True)
class ArmSpec:
    """One arm, and where its writes go.

    Separate destinations matter: the baseline opens pull requests that are wrong
    by design, and mixing them into the same repository as the real ones would
    make the evaluation itself a source of drift.
    """

    arm: Arm
    label: str
    description: str

    @property
    def writes_proof(self) -> bool:
        """Whether this arm's pull requests carry a proof block."""
        return self.arm is Arm.PROOFPR


PROOFPR = ArmSpec(
    arm=Arm.PROOFPR,
    label="proofpr",
    description="The full pipeline: exists check, scope rules, reproduction, gate, proof.",
)

BASELINE = ArmSpec(
    arm=Arm.NO_GATE,
    label="no_gate",
    description="Localize, patch, keep the suite green, open a pull request. No proof.",
)

ARMS: dict[str, ArmSpec] = {spec.label: spec for spec in (PROOFPR, BASELINE)}


def describe_difference() -> str:
    """Render the difference between the arms, for the report."""
    lines = [
        "Both arms run the same pipeline against the same cases in the same session.",
        "",
        "The baseline arm skips:",
        "",
    ]
    lines += [f"- `{step.value}`: {why}" for step, why in SKIPPED_BY_BASELINE.items()]
    return "\n".join(lines)
