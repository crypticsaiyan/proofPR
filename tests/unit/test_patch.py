"""The patch loop: refuse bad patches cheaply, retry with the failure attached."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from proofpr.guard.policy import Policy
from proofpr.steps import patch as patch_step
from proofpr.steps.patch import PatchedFile, ProposedPatch
from tests.conftest import REPO_ROOT
from tests.fixtures.sandboxes import ScriptedSandbox

VALIDKIT = REPO_ROOT / "tests" / "fixtures" / "validkit"
SOURCE = "src/validkit/dates.py"
TEST = "tests/test_repro.py"
TEST_CODE = "import pytest\n\ndef test_x():\n    assert True\n"


class ScriptedModel:
    """Returns scripted patches in order."""

    def __init__(self, *patches: ProposedPatch) -> None:
        """Build the stub."""
        self.patches = list(patches)
        self.prompts: list[str] = []

    async def complete_json(
        self, *, system: str, user: str, schema: type[Any], **kwargs: Any
    ) -> tuple[Any, Any]:
        """Return the next scripted patch, recording the prompt it answered."""
        self.prompts.append(user)
        usage = type(
            "Usage",
            (),
            {
                "tier": "strong",
                "model": "stub",
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 0.01,
            },
        )()
        return self.patches.pop(0), usage


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A worktree copy of the sample repository."""
    destination = tmp_path / "validkit"
    shutil.copytree(VALIDKIT, destination)
    return destination


def patch(content: str = "x = 1\n", path: str = SOURCE) -> ProposedPatch:
    """Build a proposed patch."""
    return ProposedPatch(files=[PatchedFile(path=path, content=content)], summary="fix it")


async def run_loop(tree: Path, policy: Policy, model: Any, sandbox: Any) -> Any:
    """Run the patch loop."""
    return await patch_step.run(
        model=model,
        sandbox=sandbox,
        policy=policy,
        worktree_path=tree,
        test_path=TEST,
        test_code=TEST_CODE,
        traceback="TypeError: bad",
        source_path=SOURCE,
        sanitized_block="<untrusted>report</untrusted>",
    )


class TestValidation:
    """A patch is refused before it costs sandbox time."""

    def test_a_reasonable_patch_is_accepted(self, tree: Path, policy: Policy) -> None:
        assert patch_step.validate(patch(), policy=policy, worktree_path=tree) is None

    def test_a_patch_outside_the_allowed_paths_is_refused(self, tree: Path, policy: Policy) -> None:
        rejection = patch_step.validate(
            patch(path="pyproject.toml"), policy=policy, worktree_path=tree
        )

        assert rejection is not None
        assert "never_touch" in rejection

    def test_a_patch_escaping_the_repository_is_refused(self, tree: Path, policy: Policy) -> None:
        rejection = patch_step.validate(
            patch(path="../../etc/passwd"), policy=policy, worktree_path=tree
        )

        assert rejection is not None
        assert "outside the repository" in rejection

    def test_a_patch_that_does_not_parse_is_refused(self, tree: Path, policy: Policy) -> None:
        rejection = patch_step.validate(patch("def broken(:"), policy=policy, worktree_path=tree)

        assert rejection is not None
        assert "does not parse" in rejection

    def test_creating_a_new_file_is_refused(self, tree: Path, policy: Policy) -> None:
        rejection = patch_step.validate(
            patch(path="src/validkit/brand_new.py"), policy=policy, worktree_path=tree
        )

        assert rejection is not None
        assert "does not exist" in rejection

    def test_touching_more_than_one_file_is_refused(self, tree: Path, policy: Policy) -> None:
        with pytest.raises(ValueError, match="at most 1"):
            ProposedPatch(
                files=[
                    PatchedFile(path=SOURCE, content="x = 1\n"),
                    PatchedFile(path="src/validkit/email.py", content="y = 2\n"),
                ]
            )


class TestLoop:
    """Three attempts, each shown why the last one failed."""

    async def test_a_working_patch_is_accepted_on_the_first_attempt(
        self, tree: Path, policy: Policy
    ) -> None:
        result = await run_loop(
            tree, policy, ScriptedModel(patch()), ScriptedSandbox(default_exit_code=0)
        )

        assert result.succeeded is True
        assert len(result.attempts) == 1
        assert result.originals[SOURCE] is not None

    async def test_a_patch_that_does_not_fix_the_test_is_retried(
        self, tree: Path, policy: Policy
    ) -> None:
        model = ScriptedModel(patch("x = 1\n"), patch("x = 2\n"))
        sandbox = ScriptedSandbox(pytest_exit_codes=[1], default_exit_code=0)

        result = await run_loop(tree, policy, model, sandbox)

        assert result.succeeded is True
        assert len(result.attempts) == 2
        # The retry must carry the actual failure, or the model repeats itself.
        assert "the reproduction test still fails" in model.prompts[1]

    async def test_a_patch_that_breaks_the_suite_is_retried_with_that_reason(
        self, tree: Path, policy: Policy
    ) -> None:
        model = ScriptedModel(patch("x = 1\n"), patch("x = 2\n"))
        sandbox = ScriptedSandbox(pytest_exit_codes=[0, 1], default_exit_code=0)

        result = await run_loop(tree, policy, model, sandbox)

        assert result.succeeded is True
        assert "the suite broke" in model.prompts[1]

    async def test_a_failed_attempt_leaves_the_worktree_as_it_was(
        self, tree: Path, policy: Policy
    ) -> None:
        original = (tree / SOURCE).read_text()
        model = ScriptedModel(patch("x = 1\n"), patch("x = 2\n"), patch("x = 3\n"))
        sandbox = ScriptedSandbox(default_exit_code=1)

        result = await run_loop(tree, policy, model, sandbox)

        assert result.succeeded is False
        assert (tree / SOURCE).read_text() == original

    async def test_three_failures_end_the_loop(self, tree: Path, policy: Policy) -> None:
        model = ScriptedModel(patch("x = 1\n"), patch("x = 2\n"), patch("x = 3\n"))

        result = await run_loop(tree, policy, model, ScriptedSandbox(default_exit_code=1))

        assert result.succeeded is False
        assert len(result.attempts) == patch_step.MAX_ATTEMPTS

    async def test_a_refused_patch_never_reaches_the_sandbox(
        self, tree: Path, policy: Policy
    ) -> None:
        model = ScriptedModel(patch(path="pyproject.toml"), patch())
        sandbox = ScriptedSandbox(default_exit_code=0)

        await run_loop(tree, policy, model, sandbox)

        # Only the accepted second patch was ever run: test, then suite.
        assert sandbox.calls == [f"pytest:{TEST}", "pytest:."]

    async def test_the_reproduction_test_is_present_for_the_whole_loop(
        self, tree: Path, policy: Policy
    ) -> None:
        await run_loop(tree, policy, ScriptedModel(patch()), ScriptedSandbox(default_exit_code=0))

        assert (tree / TEST).read_text() == TEST_CODE
