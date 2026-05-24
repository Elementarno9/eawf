"""Unit tests for :class:`eawf.kernel.state.models.McpGrant`.

Covers:

- Happy-path construction (boundary: minimal valid fields).
- Strict ``extra="forbid"`` rejection of unknown keys.
- ``scope_kind`` literal alternation (``wave``/``profile``/``global``).
- ``IdStr`` admits the ``GRANT-<n>`` convention without baking it into
  the schema pattern (kept additive per P10 W02 spec).
- ``granted_at`` enforces tz-aware datetimes via :data:`UtcDatetime`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import McpGrant

pytestmark = pytest.mark.unit


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "GRANT-1",
        "scope_kind": "wave",
        "scope_id": "P10-I01-W04",
        "server_id": "filesystem",
        "granted_at": "2026-05-08T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_mcp_grant_construct_minimal_payload() -> None:
    grant = McpGrant.model_validate(_valid_payload())
    assert grant.id == "GRANT-1"
    assert grant.scope_kind == "wave"
    assert grant.scope_id == "P10-I01-W04"
    assert grant.server_id == "filesystem"
    # Datetime parsed and tz-normalised to UTC.
    assert grant.granted_at == datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC)


def test_mcp_grant_round_trips_json_payload() -> None:
    raw = _valid_payload(scope_kind="global", scope_id="global")
    grant = McpGrant.model_validate(raw)
    dumped = grant.model_dump(mode="json")
    assert dumped["scope_kind"] == "global"
    assert dumped["scope_id"] == "global"
    # Re-validation is a fixed point.
    assert McpGrant.model_validate(dumped) == grant


def test_mcp_grant_rejects_extra_field() -> None:
    payload = _valid_payload(unexpected="oops")
    with pytest.raises(ValidationError) as exc_info:
        McpGrant.model_validate(payload)
    assert "extra" in str(exc_info.value).lower() or "unexpected" in str(exc_info.value).lower()


@pytest.mark.parametrize("bad_scope", ["wave-2", "ALL", "scope", "globally"])
def test_mcp_grant_rejects_unknown_scope_kind(bad_scope: str) -> None:
    payload = _valid_payload(scope_kind=bad_scope)
    with pytest.raises(ValidationError):
        McpGrant.model_validate(payload)


@pytest.mark.parametrize("good_scope", ["wave", "profile", "global"])
def test_mcp_grant_accepts_each_canonical_scope_kind(good_scope: str) -> None:
    grant = McpGrant.model_validate(_valid_payload(scope_kind=good_scope))
    assert grant.scope_kind == good_scope


def test_mcp_grant_id_admits_grant_n_convention() -> None:
    """The ``GRANT-<n>`` convention is enforced by the CLI, not the schema.

    The schema pattern stays :data:`IdStr` (``^\\S+$``) so the convention can
    evolve without a schema break. This test pins both halves of the contract:
    a canonical ``GRANT-7`` id matches, AND the schema admits any non-empty
    no-whitespace id (the convention is a CLI concern).
    """
    grant = McpGrant.model_validate(_valid_payload(id="GRANT-7"))
    assert grant.id == "GRANT-7"
    # Convention check: matches the documented ``^GRANT-[0-9]+$`` shape.
    assert re.fullmatch(r"GRANT-[0-9]+", grant.id) is not None
    # The schema does not lock the prefix — non-conforming ids still pass.
    other = McpGrant.model_validate(_valid_payload(id="legacy-x42"))
    assert other.id == "legacy-x42"


def test_mcp_grant_id_rejects_whitespace_per_id_str() -> None:
    payload = _valid_payload(id="GRANT 1")
    with pytest.raises(ValidationError):
        McpGrant.model_validate(payload)


def test_mcp_grant_id_rejects_empty_string() -> None:
    payload = _valid_payload(id="")
    with pytest.raises(ValidationError):
        McpGrant.model_validate(payload)


def test_mcp_grant_server_id_rejects_whitespace_per_id_str() -> None:
    payload = _valid_payload(server_id="filesystem ext")
    with pytest.raises(ValidationError):
        McpGrant.model_validate(payload)


def test_mcp_grant_granted_at_requires_timezone() -> None:
    """``UtcDatetime`` refuses naive datetimes — same contract as siblings."""
    payload = _valid_payload(granted_at="2026-05-08T00:00:00")
    with pytest.raises(ValidationError) as exc_info:
        McpGrant.model_validate(payload)
    assert "timezone" in str(exc_info.value).lower() or "tz" in str(exc_info.value).lower()


def test_mcp_grant_granted_at_normalises_non_utc_offset() -> None:
    """A +02:00 offset is accepted and stored as UTC (mirrors UtcDatetime)."""
    grant = McpGrant.model_validate(_valid_payload(granted_at="2026-05-08T02:00:00+02:00"))
    assert grant.granted_at == datetime(2026, 5, 8, 0, 0, 0, tzinfo=UTC)
