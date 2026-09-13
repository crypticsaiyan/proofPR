"""Code and policy must agree.

An operation that exists in an adapter but not in ``config/policy.yaml`` would be
refused at runtime, in production, on the one report that needed it. These tests
make that a build failure instead.
"""

from __future__ import annotations

import pytest

from proofpr.adapters import APP_ADAPTERS, HttpAdapter
from proofpr.guard.policy import Policy
from tests.fixtures.fakes import FakeDiscord, FakeGitHub, FakeLinear

FAKES = (FakeDiscord, FakeGitHub, FakeLinear)


@pytest.mark.parametrize("adapter", APP_ADAPTERS, ids=lambda cls: cls.app)
def test_every_adapter_write_is_allowlisted(adapter: type[HttpAdapter], policy: Policy) -> None:
    missing = adapter.WRITE_OPERATIONS - policy.allowed_operations(adapter.app)

    assert not missing, f"{adapter.app} can perform unallowlisted writes: {sorted(missing)}"


@pytest.mark.parametrize("adapter", APP_ADAPTERS, ids=lambda cls: cls.app)
def test_no_adapter_implements_a_denied_operation(
    adapter: type[HttpAdapter], policy: Policy
) -> None:
    overlap = adapter.WRITE_OPERATIONS & policy.denied_operations(adapter.app)

    assert not overlap, f"{adapter.app} implements denied operations: {sorted(overlap)}"


@pytest.mark.parametrize("adapter", APP_ADAPTERS, ids=lambda cls: cls.app)
def test_every_declared_write_exists_as_a_method(adapter: type[HttpAdapter]) -> None:
    for operation in adapter.WRITE_OPERATIONS:
        assert callable(getattr(adapter, operation, None)), (
            f"{adapter.app}.{operation} is declared but not implemented"
        )


@pytest.mark.parametrize("adapter", APP_ADAPTERS, ids=lambda cls: cls.app)
def test_every_allowlisted_operation_is_implemented(
    adapter: type[HttpAdapter], policy: Policy
) -> None:
    unimplemented = policy.allowed_operations(adapter.app) - adapter.WRITE_OPERATIONS

    assert not unimplemented, (
        f"policy allows {sorted(unimplemented)} for {adapter.app}, "
        "but no adapter method performs them. Remove them from the policy: the allowlist "
        "is the statement of blast radius and must not overstate it."
    )


@pytest.mark.parametrize(("real", "fake"), list(zip(APP_ADAPTERS, FAKES, strict=True)))
def test_each_fake_offers_the_same_writes_as_its_adapter(
    real: type[HttpAdapter], fake: type[object]
) -> None:
    assert real.WRITE_OPERATIONS == fake.WRITE_OPERATIONS  # type: ignore[attr-defined]

    for operation in real.WRITE_OPERATIONS:
        assert callable(getattr(fake, operation, None)), (
            f"fake {real.app} is missing {operation}, so tests using it prove less than they claim"
        )
