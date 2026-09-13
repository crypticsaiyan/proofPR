"""A tiny validation library, used as the target repository in tests.

It carries deliberate bugs. That is the point: the agent is measured on whether
it reproduces them honestly, not on whether it likes the code.
"""

from validkit.dates import parse_date
from validkit.email import validate_email
from validkit.numbers import clamp, parse_port
from validkit.text import slugify, truncate

__all__ = ["clamp", "parse_date", "parse_port", "slugify", "truncate", "validate_email"]
