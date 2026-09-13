"""Version parsing and support windows.

A report against a version the project no longer supports is not a bug to fix,
it is an upgrade to recommend. Deciding that needs comparable versions, and a
report that states no version at all must not be silently treated as supported.

Deliberately a small subset of PEP 440: release segments, optional pre-release
suffixes, and the comparison operators used in a support window. Anything more
exotic is reported as unparseable rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

VERSION_PATTERN = re.compile(
    r"""
    v?
    (?P<release>\d+(?:\.\d+)*)
    (?:
        [-.]?
        (?P<pre_kind>a|b|rc|alpha|beta|dev)
        [-.]?
        (?P<pre_number>\d+)?
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: How a report mentions its version: "on 1.4.2", "version 1.4.2", "v1.4.2".
#: The keyword must start a word. Without that, a traceback path such as
#: `/usr/lib/python3.12/json` reads as "on 3.12", and the interpreter's version
#: is taken for the project's.
MENTION_PATTERN = re.compile(
    r"(?<![\w/.])(?:version|release|running|uses?d?|using|on|v)\s*[:=]?\s*v?(\d+(?:\.\d+){1,3}(?:[-.]?(?:a|b|rc|alpha|beta|dev)\d*)?)",
    re.IGNORECASE,
)

PRE_ORDER = {"dev": -3, "alpha": -2, "a": -2, "beta": -1, "b": -1, "rc": 0}


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A comparable version."""

    release: tuple[int, ...]
    pre: tuple[int, int] | None = None

    def __str__(self) -> str:
        """Render the version back to a readable string."""
        text = ".".join(str(part) for part in self.release)
        if self.pre is not None:
            kind = next(name for name, value in PRE_ORDER.items() if value == self.pre[0])
            text = f"{text}{kind}{self.pre[1]}"
        return text

    def _key(self) -> tuple[tuple[int, ...], tuple[int, int]]:
        """Return a sortable key, padding releases so 1.4 and 1.4.0 compare equal."""
        padded = self.release + (0,) * (4 - len(self.release))
        # A release with no pre-release segment is newer than any of its own
        # pre-releases, so it sorts above them.
        return padded[:4], self.pre if self.pre is not None else (1, 0)

    def __lt__(self, other: Version) -> bool:
        """Compare two versions."""
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        """Compare two versions for equality."""
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        """Hash by the comparison key."""
        return hash(self._key())


def parse(text: str) -> Version | None:
    """Parse a version string, returning None when it is not one."""
    match = VERSION_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    pre = None
    if kind := match.group("pre_kind"):
        pre = (PRE_ORDER[kind.lower()], int(match.group("pre_number") or 0))
    return Version(release=release, pre=pre)


def find_in_text(text: str) -> Version | None:
    """Find the version a report mentions, if it mentions one.

    Returns None when there is no mention. The caller must treat that as unknown
    and ask, never as supported.
    """
    for candidate in MENTION_PATTERN.findall(text):
        if version := parse(candidate):
            return version
    return None


def satisfies(version: Version, specifier: str) -> bool:
    """Return whether a version satisfies a comma-separated specifier.

    Supports ``>=``, ``>``, ``<=``, ``<``, ``==`` and ``!=``. An unparseable
    clause raises, because silently ignoring one would widen the support window
    without anyone noticing.

    Raises:
        ValueError: A clause could not be parsed.
    """
    for clause in (part.strip() for part in specifier.split(",") if part.strip()):
        match = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*(.+)", clause)
        if not match:
            raise ValueError(f"unparseable version specifier clause: {clause!r}")
        operator, raw = match.group(1), match.group(2)
        bound = parse(raw)
        if bound is None:
            raise ValueError(f"unparseable version in specifier clause: {clause!r}")
        comparisons = {
            ">=": version >= bound,
            ">": version > bound,
            "<=": version <= bound,
            "<": version < bound,
            "==": version == bound,
            "!=": version != bound,
        }
        if not comparisons[operator]:
            return False
    return True
