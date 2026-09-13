"""Properties that must hold for every input, not only the ones we thought of.

The guard is where an attacker's text meets a decision, so its behaviour is
stated as invariants rather than examples.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from proofpr.domain.errors import GuardBlockedError
from proofpr.domain.models import WriteIntent
from proofpr.guard.egress import EgressScanner
from proofpr.guard.policy import Policy
from proofpr.guard.sanitize import CLOSE_MARKER, sanitize

text = st.text(max_size=400)


@given(operation=st.text(min_size=1, max_size=30).filter(lambda s: "." not in s))
def test_an_unqualified_operation_is_never_constructible(operation: str) -> None:
    # Either rule may reject it: unqualified names fail the validator, and names
    # that are only whitespace are stripped to nothing and fail the length rule.
    # What matters is that no such intent can exist.
    with pytest.raises(ValueError, match=r"qualified|at least 1 character"):
        WriteIntent(run_id="r", app="github", operation=operation, target="t", payload={})


@given(operation=st.text(min_size=1, max_size=20).filter(str.isidentifier))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_policy_refuses_anything_it_does_not_name(policy: Policy, operation: str) -> None:
    # The policy is immutable once loaded, so reusing one instance across
    # generated inputs cannot leak state between examples.
    intent = WriteIntent(
        run_id="r", app="github", operation=f"github.{operation}", target="t", payload={}
    )

    if operation in policy.allowed_operations("github"):
        return
    with pytest.raises(GuardBlockedError):
        policy.check(intent)


@given(body=text)
@settings(max_examples=100)
def test_a_canary_anywhere_in_a_payload_is_always_caught(body: str) -> None:
    canary = "canary-9f83b1e4c7a2d5"
    scanner = EgressScanner(canary=canary, environ={})
    intent = WriteIntent(
        run_id="r",
        app="linear",
        operation="linear.comment",
        target="t",
        payload={"body": f"{body}{canary}{body}"},
    )

    with pytest.raises(GuardBlockedError):
        scanner.scan(intent)


@given(body=text)
@settings(max_examples=100)
def test_sanitizing_never_raises_and_always_produces_one_closing_marker(body: str) -> None:
    result = sanitize(body, source="discord")

    assert result.block().count(CLOSE_MARKER) == 1


@given(body=text)
@settings(max_examples=100)
def test_sanitizing_is_idempotent_in_its_flags(body: str) -> None:
    once = sanitize(body, source="discord")
    twice = sanitize(once.text, source="discord")

    # Re-sanitizing already sanitized text must not invent new signals, which
    # would make flag counts depend on how many times text passed the boundary.
    assert set(twice.flags) <= set(once.flags) | {"marker_forgery"}
