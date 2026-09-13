"""Signature verification: the only thing standing between a URL and a command."""

from __future__ import annotations

import pytest

from proofpr.webhooks import signatures

SECRET = "s3cret-webhook-value"
BODY = b'{"action":"closed","pull_request":{"merged":true}}'


class TestGitHub:
    """`X-Hub-Signature-256`, prefixed and hex."""

    def test_a_genuine_signature_verifies(self) -> None:
        header = signatures.sign(SECRET, BODY, provider="github")

        assert signatures.verify_github(SECRET, BODY, header).valid is True

    def test_a_forged_signature_is_refused(self) -> None:
        result = signatures.verify_github(SECRET, BODY, "sha256=" + "0" * 64)

        assert result.valid is False
        assert "does not match" in result.reason

    def test_a_changed_body_no_longer_verifies(self) -> None:
        header = signatures.sign(SECRET, BODY, provider="github")

        assert signatures.verify_github(SECRET, BODY + b" ", header).valid is False

    def test_a_signature_from_another_secret_is_refused(self) -> None:
        header = signatures.sign("someone elses secret", BODY, provider="github")

        assert signatures.verify_github(SECRET, BODY, header).valid is False

    def test_a_missing_signature_is_refused(self) -> None:
        result = signatures.verify_github(SECRET, BODY, None)

        assert result.valid is False
        assert "no signature" in result.reason

    def test_a_weaker_algorithm_is_refused(self) -> None:
        # An attacker choosing the algorithm is an attacker choosing the strength.
        result = signatures.verify_github(SECRET, BODY, "sha1=" + "0" * 40)

        assert result.valid is False
        assert "not sha256" in result.reason

    def test_an_unconfigured_secret_refuses_everything(self) -> None:
        # The dangerous shape would be accepting when unconfigured. It does not.
        result = signatures.verify_github("", BODY, signatures.sign("", BODY, provider="github"))

        assert result.valid is False
        assert "no GitHub webhook secret" in result.reason


class TestLinear:
    """Bare hex digests, same rules."""

    @pytest.mark.parametrize("verify", [signatures.verify_linear])
    def test_a_genuine_signature_verifies(self, verify: object) -> None:
        header = signatures.sign(SECRET, BODY, provider="linear")

        assert verify(SECRET, BODY, header).valid is True  # type: ignore[operator]

    @pytest.mark.parametrize("verify", [signatures.verify_linear])
    def test_whitespace_around_a_header_is_tolerated(self, verify: object) -> None:
        header = signatures.sign(SECRET, BODY, provider="linear")

        assert verify(SECRET, BODY, f"  {header}\n").valid is True  # type: ignore[operator]

    @pytest.mark.parametrize("verify", [signatures.verify_linear])
    def test_an_unconfigured_secret_refuses_everything(self, verify: object) -> None:
        assert verify("", BODY, "anything").valid is False  # type: ignore[operator]

    @pytest.mark.parametrize("verify", [signatures.verify_linear])
    def test_a_github_style_prefix_is_not_accepted_here(self, verify: object) -> None:
        header = signatures.sign(SECRET, BODY, provider="github")

        assert verify(SECRET, BODY, header).valid is False  # type: ignore[operator]


def test_the_raw_body_is_what_is_signed() -> None:
    # Re-serialising JSON changes bytes. Verifying a parsed and re-encoded body
    # would fail for genuine requests and, worse, could be made to pass for
    # mismatched ones.
    import json

    # Real webhook bodies are compact and not always key-sorted, so a round trip
    # through json changes the bytes even when the value is identical.
    original = b'{"b":2,"a":1}'
    reserialised = json.dumps(json.loads(original)).encode()
    assert reserialised != original
    header = signatures.sign(SECRET, original, provider="github")

    assert signatures.verify_github(SECRET, original, header).valid is True
    assert signatures.verify_github(SECRET, reserialised, header).valid is False
