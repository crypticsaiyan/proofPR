"""Publishing: branch, commit, draft pull request.

Section 3.2 of AGENTS.md, the part where the agent finally writes to someone's
repository. Every write here goes through the guard like every other write, and
the order is chosen so a failure leaves the least mess: the branch first, then
the test, then the fix, then the pull request that ties them together.

The pull request opens as a draft. It leaves draft only when the checks API says
CI passed, which is a different claim from "the tests passed in our sandbox" and
the only one the proof block is allowed to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from proofpr.domain.models import WriteIntent

#: Prefix for commit subjects, so the history says what wrote them.
COMMIT_PREFIX = "fix"


@dataclass
class Published:
    """What publishing produced."""

    branch: str
    pr_number: str | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    commits: list[str] = field(default_factory=list)


def branch_name(run_id: str, prefix: str = "proofpr/") -> str:
    """Return this run's branch name.

    The run id is in the name so a stray branch can always be traced back to the
    run that made it, and so a retry finds its own branch rather than making a
    second one.
    """
    return f"{prefix}{run_id}"


def commit_subject(summary: str, exception: str | None) -> str:
    """Build a Conventional Commits subject line from the patch summary."""
    text = (summary or f"handle {exception or 'the reported input'} correctly").strip()
    text = text[0].lower() + text[1:] if text else text
    subject = f"{COMMIT_PREFIX}: {text.rstrip('.')}"
    return subject[:72]


def pr_body(
    *,
    proof_block: str,
    rationale: str,
    report_url: str | None,
    linear_url: str | None,
    linear_identifier: str | None,
    run_id: str,
) -> str:
    """Build the pull request body.

    The proof block leads. A reviewer opening this should see the evidence before
    the prose, because the evidence is the part they can check.
    """
    parts = [proof_block, ""]
    if rationale:
        parts += ["## Why this is the cause", "", rationale, ""]

    inputs = []
    if report_url:
        inputs.append(f"Reported in [chat]({report_url})")
    if linear_url and linear_identifier:
        inputs.append(f"Tracked as [{linear_identifier}]({linear_url})")
    if inputs:
        parts += ["## Inputs", "", " · ".join(inputs), ""]

    parts += [
        "---",
        "",
        "Opened by ProofPR. It cannot open a pull request it has not proved: every "
        "row above is a command you can rerun.",
        "",
        f"<!-- proofpr-run:{run_id} -->",
    ]
    return "\n".join(parts)


async def run(
    *,
    github: Any,  # noqa: ANN401 - a GitHubPort
    write: Any,  # noqa: ANN401 - the pipeline's guarded write helper
    intent_factory: Any,  # noqa: ANN401
    run_id: str,
    branch_prefix: str,
    default_branch: str,
    files: dict[str, str],
    commit_summary: str,
    exception: str | None,
    title: str,
    body: str,
) -> Published:
    """Create the branch, commit each file, and open the draft pull request.

    Args:
        github: The GitHub adapter.
        write: The pipeline's write helper, which guards, records, and reads back.
        intent_factory: Builds a :class:`WriteIntent` bound to this run.
        run_id: The run.
        branch_prefix: Required branch prefix.
        default_branch: Base for the branch and the pull request.
        files: Repository path to new contents. The test is committed before the
            fix, so the branch history shows the failure before the fix even
            without reading the pull request.
        commit_summary: One line, used for the fix commit subject.
        exception: The reproduced exception, used when there is no summary.
        title: Pull request title.
        body: Pull request body, proof block first.

    Returns:
        What was published.
    """
    branch = branch_name(run_id, branch_prefix)
    published = Published(branch=branch)

    created = await write(
        intent_factory(
            "github.create_branch",
            target=f"{github.slug}@{branch}",
            branch=branch,
            **{"from": default_branch},
        )
    )
    published.head_sha = created.remote_id

    for path, content in files.items():
        is_test = path.startswith("tests/")
        subject = (
            f"test: add a failing test for {exception or 'the reported input'}"[:72]
            if is_test
            else commit_subject(commit_summary, exception)
        )
        result = await write(
            intent_factory(
                "github.push",
                target=f"{github.slug}@{branch}:{path}",
                branch=branch,
                path=path,
                content=content,
                message=subject,
            )
        )
        published.commits.append(result.remote_id)
        published.head_sha = result.remote_id

    opened = await write(
        intent_factory(
            "github.open_pr",
            target=f"{github.slug}@{branch}",
            title=title,
            head=branch,
            base=default_branch,
            body=body,
            draft=True,
        )
    )
    published.pr_number = opened.remote_id
    published.pr_url = opened.url
    return published


def pr_title(summary: str, exception: str | None, function: str | None) -> str:
    """Build a pull request title that names the defect, not the reporter."""
    if summary:
        return commit_subject(summary, exception)
    where = f" in {function}" if function else ""
    return f"{COMMIT_PREFIX}: handle {exception or 'invalid input'}{where}"


__all__ = [
    "Published",
    "WriteIntent",
    "branch_name",
    "commit_subject",
    "pr_body",
    "pr_title",
    "run",
]
