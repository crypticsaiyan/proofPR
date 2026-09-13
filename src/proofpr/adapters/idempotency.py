"""Finding a write that already happened.

A write whose response was lost may or may not have landed. Retrying it blindly
creates a second issue, a second pull request, or a second reply; giving up
leaves an orphan the run never records. The only safe move is to look first.

Every body ProofPR writes carries the run marker, so a write can be found again.
The marker alone is not enough, though: one run may comment on the same issue
twice (a duplicate note and a receipt), and matching the marker would mistake
one for the other. So a match also requires the start of the intended body,
compared after stripping markup, because applications rewrite formatting and a
byte comparison would miss writes that are really there.
"""

from __future__ import annotations

import re

from proofpr.domain.models import WriteIntent

#: How much of the intended body must appear. Long enough to tell a receipt
#: from a duplicate note, short enough to survive an application truncating or
#: re-flowing the text.
FINGERPRINT_CHARS = 48

_NOT_ALNUM = re.compile(r"[^0-9a-z]+")


def normalise(text: str) -> str:
    """Lowercase and drop everything but letters and digits."""
    return _NOT_ALNUM.sub("", text.lower())


def carries(existing: str, intent: WriteIntent, body: str) -> bool:
    """Return whether `existing` is the write this intent describes.

    Args:
        existing: Text read back from the application.
        intent: The write being looked for.
        body: The body the intent would write, without its marker.
    """
    if intent.marker not in existing:
        return False
    return normalise(body)[:FINGERPRINT_CHARS] in normalise(existing)


__all__ = ["FINGERPRINT_CHARS", "carries", "normalise"]
