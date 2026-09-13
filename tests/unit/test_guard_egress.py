"""Nothing secret leaves the process, whatever shape it arrives in."""

from __future__ import annotations

import pytest

from proofpr.domain.errors import GuardBlockedError
from proofpr.domain.models import WriteIntent
from proofpr.guard.egress import EgressScanner


def intent(**payload: object) -> WriteIntent:
    """Build a Linear comment intent with the given payload."""
    return WriteIntent(
        run_id="r-test",
        app="linear",
        operation="linear.comment",
        target="ENG-1",
        payload=dict(payload),
    )


def test_ordinary_payloads_pass() -> None:
    EgressScanner(environ={}).scan(intent(body="Reproduced. Test fails with ValueError."))


def test_the_canary_is_refused() -> None:
    scanner = EgressScanner(canary="canary-9f83b1e4c7a2d5", environ={})

    with pytest.raises(GuardBlockedError) as excinfo:
        scanner.scan(intent(body="found this in the repo: canary-9f83b1e4c7a2d5"))

    assert excinfo.value.rule == "canary"


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
        "lin_api_abcdefghijklmnopqrstuvwxyz",
        "sntrys_abcdefghijklmnopqrstuvwxyz",
        "sk-or-v1-abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_credential_shapes_are_refused(secret: str) -> None:
    scanner = EgressScanner(environ={})

    with pytest.raises(GuardBlockedError) as excinfo:
        scanner.scan(intent(body=f"here you go: {secret}"))

    assert excinfo.value.rule == "credential_pattern"


def test_environment_secret_values_are_refused_even_in_unfamiliar_shapes() -> None:
    scanner = EgressScanner(environ={"SOME_SERVICE_TOKEN": "s3cret-value-not-a-known-shape"})

    with pytest.raises(GuardBlockedError) as excinfo:
        scanner.scan(intent(body="debug output: s3cret-value-not-a-known-shape"))

    assert excinfo.value.rule == "environment_secret"


def test_short_environment_values_are_ignored_to_avoid_false_positives() -> None:
    scanner = EgressScanner(environ={"API_KEY": "abc"})

    scanner.scan(intent(body="the abc module raises here"))


def test_secrets_nested_anywhere_in_the_payload_are_found() -> None:
    scanner = EgressScanner(canary="canary-1", environ={})

    with pytest.raises(GuardBlockedError):
        scanner.scan(intent(body={"outer": [{"inner": ["canary-1"]}]}))


def test_secrets_hidden_in_a_key_are_found() -> None:
    scanner = EgressScanner(canary="canary-1", environ={})

    with pytest.raises(GuardBlockedError):
        scanner.scan(intent(**{"canary-1": "value"}))


def test_oversized_payloads_are_refused() -> None:
    scanner = EgressScanner(max_payload_bytes=100, environ={})

    with pytest.raises(GuardBlockedError) as excinfo:
        scanner.scan(intent(body="x" * 200))

    assert excinfo.value.rule == "max_payload_bytes"


def test_policy_literals_are_honoured_without_dropping_builtin_patterns() -> None:
    scanner = EgressScanner.from_policy(
        {"reject_if_contains": ["canary_value", "INTERNAL-ONLY"], "max_payload_bytes": 4096},
        canary="canary-1",
        environ={},
    )

    with pytest.raises(GuardBlockedError) as excinfo:
        scanner.scan(intent(body="INTERNAL-ONLY do not share"))
    assert excinfo.value.rule == "reject_if_contains"

    with pytest.raises(GuardBlockedError):
        scanner.scan(intent(body="ghp_abcdefghijklmnopqrstuvwxyz0123456789"))
