"""Stack and report fingerprinting.

Duplicate detection starts here, deterministically, before any model is asked
anything. Two reports of the same crash usually share an exception type and a
top in-application frame even when the prose has nothing in common, and prose
similarity alone confuses two different bugs in the same function.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Python exception names as they appear in a traceback.
EXCEPTION_PATTERN = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning))\b")

#: ``File "path", line N, in func`` and the shorter ``path in func`` culprit form.
FRAME_PATTERN = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
CULPRIT_PATTERN = re.compile(r"^(?P<path>[\w./\\-]+\.py) in (?P<function>\S+)")

#: Words that appear in every bug report and carry no signal for matching.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doesn",
        "for",
        "from",
        "get",
        "gets",
        "getting",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "me",
        "my",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "they",
        "this",
        "to",
        "try",
        "trying",
        "use",
        "used",
        "using",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "bug",
        "issue",
        "crash",
        "error",
        "problem",
        "happens",
        "happening",
        "when",
    ]
)

MIN_TERM_LENGTH = 3


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A deterministic identity for a crash."""

    exception: str | None
    path: str | None
    function: str | None
    terms: frozenset[str]

    @property
    def digest(self) -> str:
        """Return a stable short hash of the structural parts.

        Only the exception, path, and function contribute. Prose terms are
        deliberately excluded: they are useful for ranking candidates and far too
        unstable to be part of an identity.
        """
        material = "|".join(part or "" for part in (self.exception, self.path, self.function))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @property
    def is_structural(self) -> bool:
        """Whether the fingerprint has anything beyond prose to match on."""
        return bool(self.exception or (self.path and self.function))

    def similarity(self, other: Fingerprint) -> float:
        """Score two fingerprints between 0 and 1.

        The structural parts dominate. Prose contributes at most a third, so a
        paraphrased duplicate still scores highly while two unrelated reports
        that share vocabulary do not.
        """
        score = 0.0
        if self.exception and self.exception == other.exception:
            score += 0.35
        if self.path and self.path == other.path:
            score += 0.2
        if self.function and self.function == other.function:
            score += 0.12
        union = self.terms | other.terms
        if union:
            score += 0.33 * len(self.terms & other.terms) / len(union)
        return round(min(score, 1.0), 4)


#: The standard library's location in a traceback: ``.../lib/python3.12/json/...``
#: on POSIX, ``...\\Lib\\json\\...`` on Windows. Installed packages live below it in
#: ``site-packages`` and are deliberately not matched, because the library under
#: test is often installed there in the reporter's environment.
STDLIB_PATTERN = re.compile(r"[/\\]lib[/\\]python\d+(?:\.\d+)?[/\\]|[/\\]Lib[/\\]", re.IGNORECASE)


def is_library_path(path: str) -> bool:
    """Whether a traceback frame is interpreter code rather than code a report is about."""
    if path.startswith("<"):  # <frozen importlib._bootstrap>, <string>
        return True
    if "site-packages" in path or "dist-packages" in path:
        return False
    return bool(STDLIB_PATTERN.search(path))


def normalise_path(path: str) -> str:
    """Reduce a file path to a comparable form.

    Site-packages prefixes, absolute roots, and working directory differences
    make the same file look different across two reports of the same crash.
    """
    cleaned = path.replace("\\", "/").strip().lstrip("./")
    for marker in ("site-packages/", "src/"):
        index = cleaned.find(marker)
        if index != -1:
            cleaned = cleaned[index + len(marker) :]
            break
    return cleaned


def extract_terms(text: str) -> frozenset[str]:
    """Return the meaningful lowercase words in a report."""
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    return frozenset(
        word for word in words if word not in STOPWORDS and len(word) >= MIN_TERM_LENGTH
    )


def from_text(text: str) -> Fingerprint:
    """Fingerprint a report body, traceback included when it has one."""
    exception = match.group(1) if (match := EXCEPTION_PATTERN.search(text)) else None

    path = function = None
    frames = FRAME_PATTERN.findall(text)
    if frames:
        # The innermost frame is the last one printed, but in a real traceback
        # that is usually the standard library or a dependency (`json/decoder.py`
        # raising on the project's bad call). The code at fault is the innermost
        # frame that belongs to the project, so library frames are skipped when
        # any project frame exists.
        project = [frame for frame in frames if not is_library_path(frame[0])]
        raw_path, _, raw_function = (project or frames)[-1]
        path, function = normalise_path(raw_path), raw_function
    elif culprit := CULPRIT_PATTERN.search(text.strip()):
        path = normalise_path(culprit.group("path"))
        function = culprit.group("function")

    return Fingerprint(exception=exception, path=path, function=function, terms=extract_terms(text))


def search_terms(fingerprint: Fingerprint, *, limit: int = 5) -> list[str]:
    """Return query terms for searching other applications, most specific first."""
    terms: list[str] = []
    if fingerprint.exception:
        terms.append(fingerprint.exception)
    if fingerprint.function:
        terms.append(fingerprint.function)
    if fingerprint.path:
        terms.append(fingerprint.path.rsplit("/", 1)[-1])
    terms.extend(sorted(fingerprint.terms, key=len, reverse=True))
    return terms[:limit]
