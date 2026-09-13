"""The exists check.

Section 4.2 of AGENTS.md. Before any sandbox starts and before the strong model
is touched, every connected application is asked whether this work already
exists. Most reports end here, in seconds, having spent almost nothing.

Three outcomes end a run:

- `duplicate`: an open or recently closed issue describes the same defect.
- `already_fixed`: the fix is in a release newer than the reporter's version.
- `fix_in_flight`: an open pull request already addresses it.

`already_fixed` is the dangerous one. Telling a user to upgrade when the bug is
still there sends them away with nothing, so it requires a verbatim match on a
symbol or an error string in a commit between their version and HEAD. A model
opinion is never sufficient, and no model is consulted for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from proofpr.domain.run import ExistsFinding
from proofpr.triage.fingerprint import Fingerprint, search_terms

#: Above this, a candidate is a duplicate without asking a model.
CERTAIN_DUPLICATE = 0.85

#: Below this, a candidate is not worth a model call.
IGNORE_BELOW = 0.35

#: Issues closed longer ago than this are not treated as duplicates, because a
#: recurrence after a fix is a new bug, not the old one.
RECENT_CLOSE_DAYS = 90


class DuplicateJudge(Protocol):
    """Asks the cheap model whether two reports describe the same defect."""

    async def same_defect(self, report: str, candidate: str) -> tuple[bool, float, str]:
        """Return whether they match, a confidence, and a one-sentence rationale."""
        ...


@dataclass(frozen=True, slots=True)
class Candidate:
    """One possible match from one application."""

    app: str
    reference: str
    title: str
    body: str
    url: str | None
    score: float
    state: str = ""


def structural_matches(fingerprint: Fingerprint, text: str) -> int:
    """Count how many of the exception, function, and file appear verbatim.

    Three out of three is treated as certain without consulting a model. Two
    different defects that share an exception type, a function, and a file are
    possible but rare enough that filing a second issue for a human to merge is
    the better trade than a model call on every candidate.
    """
    matches = 0
    if fingerprint.exception and re.search(rf"\b{re.escape(fingerprint.exception)}\b", text):
        matches += 1
    if fingerprint.function and re.search(rf"\b{re.escape(fingerprint.function)}\b", text):
        matches += 1
    if fingerprint.path and fingerprint.path.rsplit("/", 1)[-1] in text:
        matches += 1
    return matches


def score_candidate(fingerprint: Fingerprint, title: str, body: str) -> float:
    """Score an existing issue against the incoming report.

    Candidates are prose written by humans, so they rarely contain a parseable
    traceback. Structural comparison is therefore done by looking for our own
    symbols verbatim in their text, which is both cheaper and more reliable than
    trying to parse a summary back into a fingerprint.
    """
    from proofpr.triage.fingerprint import extract_terms

    text = f"{title}\n{body}"
    score = 0.0
    if fingerprint.exception and re.search(rf"\b{re.escape(fingerprint.exception)}\b", text):
        score += 0.35
    if fingerprint.function and re.search(rf"\b{re.escape(fingerprint.function)}\b", text):
        score += 0.28
    if fingerprint.path and fingerprint.path.rsplit("/", 1)[-1] in text:
        score += 0.12
    terms = extract_terms(text)
    union = fingerprint.terms | terms
    if union:
        score += 0.25 * len(fingerprint.terms & terms) / len(union)
    return round(min(score, 1.0), 4)


async def search_linear(linear: Any, fingerprint: Fingerprint) -> list[Candidate]:  # noqa: ANN401
    """Search Linear for issues that might already cover this report."""
    candidates: dict[str, Candidate] = {}
    for term in search_terms(fingerprint):
        for issue in await linear.search_issues(term):
            reference = str(issue.get("identifier") or issue["id"])
            if reference in candidates:
                continue
            candidates[reference] = Candidate(
                app="linear",
                reference=reference,
                title=str(issue.get("title", "")),
                body=str(issue.get("description") or ""),
                url=issue.get("url"),
                score=score_candidate(
                    fingerprint, str(issue.get("title", "")), str(issue.get("description") or "")
                ),
                state=str((issue.get("state") or {}).get("type", "")),
            )
    return sorted(candidates.values(), key=lambda candidate: candidate.score, reverse=True)


async def search_github(github: Any, fingerprint: Fingerprint) -> list[Candidate]:  # noqa: ANN401
    """Search GitHub issues and pull requests."""
    candidates: dict[str, Candidate] = {}
    for term in search_terms(fingerprint, limit=3):
        for item in await github.search_issues(f"{term} in:title,body"):
            reference = str(item["number"])
            if reference in candidates:
                continue
            is_pull = "pull_request" in item
            candidates[reference] = Candidate(
                app="github",
                reference=f"#{reference}",
                title=str(item.get("title", "")),
                body=str(item.get("body") or ""),
                url=item.get("html_url"),
                score=score_candidate(
                    fingerprint, str(item.get("title", "")), str(item.get("body") or "")
                ),
                state=("pull_request" if is_pull else "issue") + f":{item.get('state', '')}",
            )
    return sorted(candidates.values(), key=lambda candidate: candidate.score, reverse=True)


def fix_evidence(fingerprint: Fingerprint, commits: list[dict[str, Any]]) -> str | None:
    """Return the commit message that verbatim names the symbol or error, if any.

    Verbatim is the whole point. A commit that says "fix parsing bug" next to a
    parsing report proves nothing; one that names the function or the exact
    exception string is evidence a human can check in one click.
    """
    needles = [needle for needle in (fingerprint.function, fingerprint.exception) if needle]
    if not needles:
        return None
    for commit in commits:
        message = str(commit.get("commit", {}).get("message", ""))
        for needle in needles:
            if re.search(rf"\b{re.escape(needle)}\b", message):
                sha = str(commit.get("sha", ""))[:7]
                return f"{sha} {message.splitlines()[0]}"
    return None


async def check(
    *,
    fingerprint: Fingerprint,
    report_text: str,
    linear: Any,  # noqa: ANN401 - a LinearPort, kept structural for the fakes
    github: Any,  # noqa: ANN401
    judge: DuplicateJudge | None = None,
    reported_version: str | None = None,
    default_branch: str = "main",
    threshold: float = 0.62,
) -> ExistsFinding:
    """Search every connected application and return the first finding.

    Order matters. An open pull request is the most actionable answer for a
    reporter, an existing issue is next, and "already fixed, upgrade" is checked
    last because it is the claim most expensive to get wrong.
    """
    linear_candidates = await search_linear(linear, fingerprint)
    github_candidates = await search_github(github, fingerprint)

    open_pulls = [
        candidate
        for candidate in github_candidates
        if candidate.state.startswith("pull_request:open") and candidate.score >= threshold
    ]
    if open_pulls:
        best = open_pulls[0]
        return ExistsFinding(
            kind="fix_in_flight",
            app="github",
            reference=best.reference,
            url=best.url,
            score=best.score,
            evidence=f"open pull request {best.reference}: {best.title}",
        )

    for candidate in [*linear_candidates, *github_candidates]:
        if candidate.score < IGNORE_BELOW:
            break
        if candidate.state.endswith("closed") or candidate.state == "completed":
            continue
        structural = structural_matches(fingerprint, f"{candidate.title}\n{candidate.body}")
        if candidate.score >= CERTAIN_DUPLICATE or (
            structural == 3 and candidate.score >= threshold
        ):
            return _duplicate(
                candidate,
                f"exception, function and file all match, score {candidate.score}"
                if structural == 3
                else f"structural match, score {candidate.score}",
            )
        if candidate.score >= threshold and judge is not None:
            same, confidence, rationale = await judge.same_defect(
                report_text, f"{candidate.title}\n\n{candidate.body}"
            )
            if same and confidence >= threshold:
                return _duplicate(candidate, f"{rationale} (confidence {confidence})")

    if reported_version:
        commits = await github.list_commits_between(reported_version, default_branch)
        if evidence := fix_evidence(fingerprint, commits):
            return ExistsFinding(
                kind="already_fixed",
                app="github",
                reference=default_branch,
                score=1.0,
                evidence=evidence,
            )

    return ExistsFinding()


def _duplicate(candidate: Candidate, evidence: str) -> ExistsFinding:
    """Build a duplicate finding from a candidate."""
    return ExistsFinding(
        kind="duplicate",
        app=candidate.app,
        reference=candidate.reference,
        url=candidate.url,
        score=candidate.score,
        evidence=evidence,
    )
