"""A stand-in for the model, so the harness can run without credentials.

Everything except the model is real when this is used: the container sandbox,
the reproduction gate, the proof checks, the guard, the ledger, and the report
generator. Only the three questions the model would answer are answered from
fixed files in `scripted/`.

What this measures is the harness. It cannot measure triage accuracy, because it
is handed the ground truth, and it cannot measure injection resistance, because
a file on disk was never going to follow an instruction in a bug report. Reports
generated from it are labelled `scripted-stub` and carry a banner saying so, and
nothing produced this way may be quoted as a result.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from proofpr.adapters.openrouter import ModelResponse
from proofpr.steps.patch import PatchedFile, ProposedPatch
from proofpr.steps.test_synth import SynthesizedTest
from proofpr.triage.intent import IntentVerdict
from schema import Case

#: Where the fixed answers live.
ANSWERS_DIR = Path(__file__).parent / "scripted"

#: The module a case is about, taken from the traceback the reporter pasted.
SOURCE_PATTERN = re.compile(r"src/(?P<package>[\w.]+)/(?P<module>\w+)\.py")

#: Used when a case names no source file. Every dataset case that reaches a
#: patch names one, so this only keeps the stub total rather than being a guess
#: that matters.
DEFAULT_MODULE = "dates"


class ScriptedModel:
    """Answers the pipeline's questions from files, at zero cost."""

    def __init__(self, case: Case, *, answers_dir: Path = ANSWERS_DIR) -> None:
        """Bind the stub to one case."""
        self.case = case
        self.answers_dir = answers_dir
        self.calls: list[str] = []

    @property
    def module(self) -> str:
        """The module this case's traceback points at."""
        match = SOURCE_PATTERN.search(self.case.text)
        return match.group("module") if match else DEFAULT_MODULE

    @property
    def source_path(self) -> str:
        """The path the patch replaces."""
        match = SOURCE_PATTERN.search(self.case.text)
        package = match.group("package") if match else "validkit"
        return f"src/{package}/{self.module}.py"

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[Any],
        tier: str = "cheap",
        max_tokens: int = 2048,
    ) -> tuple[Any, ModelResponse]:
        """Return the scripted answer for the requested schema."""
        del system, user, max_tokens  # a stub reads neither prompt
        self.calls.append(schema.__name__)
        return self._answer(schema), ModelResponse(
            model="scripted-stub",
            tier=tier,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            cost_known=True,
            prompt_version="scripted-stub",
            retried=False,
        )

    def _answer(self, schema: type[Any]) -> Any:  # noqa: ANN401 - the port returns Any
        """Build one answer, by schema."""
        if schema is IntentVerdict:
            return IntentVerdict(intent=self.case.intent, confidence=0.95)
        if schema is SynthesizedTest:
            return SynthesizedTest(
                path=f"tests/test_repro_{self.module}.py", code=self._read("tests")
            )
        if schema is ProposedPatch:
            return ProposedPatch(
                files=[PatchedFile(path=self.source_path, content=self._read("fixes"))],
                summary=f"validate the input before using it in {self.module}",
                rationale="scripted answer, not a model decision",
            )
        if schema.__name__ == "Verdict":
            # The duplicate question. Answered from ground truth, which is the
            # clearest possible statement that this stub measures nothing about
            # duplicate detection.
            return schema(
                same_defect=self.case.duplicate_of is not None,
                confidence=0.95,
                rationale="scripted answer",
            )
        required = {
            name: "scripted" for name, field in schema.model_fields.items() if field.is_required()
        }
        return schema(**required)

    def _read(self, kind: str) -> str:
        """Read a scripted file, falling back to the default module.

        Raises:
            FileNotFoundError: There is no scripted answer of that kind at all.
        """
        path = self.answers_dir / kind / f"{self.module}.py"
        if not path.is_file():
            path = self.answers_dir / kind / f"{DEFAULT_MODULE}.py"
        return path.read_text(encoding="utf-8")


__all__ = ["ANSWERS_DIR", "ScriptedModel"]
