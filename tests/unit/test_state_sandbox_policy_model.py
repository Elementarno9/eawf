"""Unit tests for :class:`eawf.sandbox.policy.SandboxPolicy`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.sandbox.policy import SandboxPolicy, allocate_policy_id


def test_sandbox_policy_minimal_round_trip() -> None:
    p = SandboxPolicy(
        id="POL-1",
        scope_kind="wave",
        scope_id="P12-I01-W01",
        granted_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    dumped = p.model_dump(mode="json")
    rehydrated = SandboxPolicy.model_validate(dumped)
    assert rehydrated.id == "POL-1"
    assert rehydrated.scope_kind == "wave"
    assert rehydrated.scope_id == "P12-I01-W01"
    assert rehydrated.allowed_tools == []
    assert rehydrated.denied_tools == []


def test_sandbox_policy_with_allow_and_deny_lists() -> None:
    p = SandboxPolicy(
        id="POL-1",
        scope_kind="profile",
        scope_id="research",
        allowed_tools=["Read", "Edit", "Bash"],
        denied_tools=["Write"],
        granted_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    assert p.allowed_tools == ["Read", "Edit", "Bash"]
    assert p.denied_tools == ["Write"]


def test_sandbox_policy_unknown_scope_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy(
            id="POL-1",
            scope_kind="namespace",  # type: ignore[arg-type]
            scope_id="x",
            granted_at=datetime(2026, 5, 8, tzinfo=UTC),
        )


def test_sandbox_policy_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        SandboxPolicy.model_validate(
            {
                "id": "POL-1",
                "scope_kind": "wave",
                "scope_id": "P12-I01-W01",
                "granted_at": "2026-05-08T00:00:00Z",
                "unknown": "field",
            }
        )


def test_allocate_policy_id_empty_pool_yields_pol_1() -> None:
    assert allocate_policy_id(None) == "POL-1"
    assert allocate_policy_id({}) == "POL-1"


def test_allocate_policy_id_picks_max_plus_one() -> None:
    pool = {
        "POL-1": _stub_policy("POL-1"),
        "POL-3": _stub_policy("POL-3"),
    }
    assert allocate_policy_id(pool) == "POL-4"


def _stub_policy(pid: str) -> SandboxPolicy:
    return SandboxPolicy(
        id=pid,
        scope_kind="wave",
        scope_id="P00-I01-W01",
        granted_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
