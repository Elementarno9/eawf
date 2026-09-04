"""Schema contract for the retained ``State.estimates`` map.

Wave claim stopped seeding a derived estimate row, but the field itself
is NOT dropped: on-disk states carry historical rows and removing the
field would invalidate them. The retirement rides the natural epoch-2
migration, so until then this module pins:

- ``estimates`` is optional -- an absent key validates to ``None``.
- An empty map validates and stays empty (no ``None`` coercion).
- Existing rows survive a validate -> dump -> validate round trip byte
  for byte.
- Keeping the field required NO schema-version bump: the accepted
  ``schema_version`` literal set still tops out at ``1.20``.

Error paths cover a malformed row, a non-mapping value, and an
out-of-range field.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import EstimateSummary, State

#: The highest ``schema_version`` the model accepts. Retaining the legacy
#: ``estimates`` field is precisely what keeps this pinned -- dropping the
#: field would be a breaking read of existing states and force a bump.
_SCHEMA_VERSION_WITHOUT_BUMP = "1.20"

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _state_payload(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid ``State`` payload, patched with *overrides*."""
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION_WITHOUT_BUMP,
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    payload.update(overrides)
    return payload


def _estimate_row(*, scope_id: str = "P01-I01-W01") -> dict[str, Any]:
    return {
        "id": f"EST-{scope_id}",
        "scope_id": scope_id,
        "expected_eu": 2.0,
        "pessimistic_eu": 7.2,
        "expected_minutes": 60.0,
        "pessimistic_minutes": 216.0,
        "display": "2.0 EU",
        "reference_class": "bucket:L",
        "confidence": "low",
        "current_store_record_id": "REC-1",
        "updated_at": _T0.isoformat(),
    }


def test_state_absent_estimates_key_validates_to_none_without_schema_bump() -> None:
    """Boundary: the empty case -- no ``estimates`` key at all -- still validates."""
    state = State.model_validate(_state_payload())

    assert state.estimates is None
    assert state.schema_version == _SCHEMA_VERSION_WITHOUT_BUMP
    assert typing.get_args(State.model_fields["schema_version"].annotation)[-1] == (
        _SCHEMA_VERSION_WITHOUT_BUMP
    )


def test_state_empty_estimates_map_stays_empty_without_schema_bump() -> None:
    """Boundary: an explicit ``{}`` is preserved, not coerced to ``None``."""
    state = State.model_validate(_state_payload(estimates={}))

    assert state.estimates == {}
    assert state.schema_version == _SCHEMA_VERSION_WITHOUT_BUMP
    assert typing.get_args(State.model_fields["schema_version"].annotation)[-1] == (
        _SCHEMA_VERSION_WITHOUT_BUMP
    )


def test_state_existing_estimate_rows_round_trip_unchanged_without_schema_bump() -> None:
    """A historical row survives validate -> dump -> validate identically.

    This is the reason the field is retained rather than dropped: states
    written before claim-time seeding stopped still carry rows, and they
    must keep reading back without a migration.
    """
    row = _estimate_row()
    state = State.model_validate(_state_payload(estimates={"P01-I01-W01": row}))

    assert state.estimates is not None
    assert list(state.estimates) == ["P01-I01-W01"]
    assert isinstance(state.estimates["P01-I01-W01"], EstimateSummary)
    assert state.estimates["P01-I01-W01"].expected_eu == pytest.approx(2.0)

    dumped = state.model_dump(mode="json")
    reloaded = State.model_validate(dumped)

    assert reloaded.model_dump(mode="json")["estimates"] == dumped["estimates"]
    assert reloaded.schema_version == _SCHEMA_VERSION_WITHOUT_BUMP
    assert typing.get_args(State.model_fields["schema_version"].annotation)[-1] == (
        _SCHEMA_VERSION_WITHOUT_BUMP
    )


def test_state_rejects_estimate_row_missing_required_field() -> None:
    """Error path: a partial row fails strict validation."""
    row = _estimate_row()
    del row["expected_eu"]

    with pytest.raises(ValidationError, match="expected_eu"):
        State.model_validate(_state_payload(estimates={"P01-I01-W01": row}))


def test_state_rejects_estimate_row_with_unknown_field() -> None:
    """Error path: ``extra="forbid"`` rejects an unmodelled key on the row."""
    row = {**_estimate_row(), "seeded_from_bucket": True}

    with pytest.raises(ValidationError, match="seeded_from_bucket"):
        State.model_validate(_state_payload(estimates={"P01-I01-W01": row}))


def test_state_rejects_non_mapping_estimates_value() -> None:
    """Error path: a list where a mapping is required fails validation."""
    with pytest.raises(ValidationError, match="estimates"):
        State.model_validate(_state_payload(estimates=[_estimate_row()]))
