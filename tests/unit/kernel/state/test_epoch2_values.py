"""Epoch-2 shared value objects: origin, reference, and hold.

Three cross-field rules are pinned here. An origin's discriminator
decides which source fields it may carry, so a native record claiming a
source digest or a legacy record missing one is refused. A reference's
kind must agree with the kind its URN addresses, so a consumer cannot
dispatch on one and resolve on the other. A hold cannot be released
before it was placed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import EntityKind
from eawf.kernel.state.epoch2 import EntityOrigin, EntityRef, Hold

pytestmark = pytest.mark.unit

TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
MILESTONE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"

_NATIVE: dict[str, Any] = {"kind": "native", "mapping_basis": "native", "confidence": "exact"}


def _legacy_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid legacy origin."""
    fields: dict[str, Any] = {
        "kind": "legacy",
        "source_schema_version": "v1.20",
        "source_kind": "wave",
        "source_id": "legacy-wave-row",
        "source_digest": "sha256:" + "e" * 64,
        "mapping_basis": "mechanical",
        "confidence": "supported",
    }
    fields.update(overrides)
    return fields


def _hold_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid, unreleased hold on a Track."""
    fields: dict[str, Any] = {
        "hold_id": "5b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "scope": {"kind": "track", "urn": TRACK_URN},
        "reason": {
            "code": "provider-outage",
            "message": "The provider stopped responding.",
        },
        "created_by": {"principal_kind": "operator", "principal_id": "OP-0001"},
        "created_at": "2026-09-08T00:00:00Z",
    }
    fields.update(overrides)
    return fields


# ---- EntityOrigin -----------------------------------------------------------


def test_entity_origin_native_validates_without_source_fields() -> None:
    origin = EntityOrigin.model_validate(_NATIVE)
    assert origin.kind == "native"
    assert origin.source_digest is None


def test_entity_origin_legacy_validates_with_every_source_field() -> None:
    origin = EntityOrigin.model_validate(_legacy_fields())
    assert origin.kind == "legacy"
    assert origin.source_id == "legacy-wave-row"


def test_entity_origin_legacy_accepts_a_source_urn() -> None:
    origin = EntityOrigin.model_validate(_legacy_fields(source_urn=TASK_URN))
    assert str(origin.source_urn) == TASK_URN


@pytest.mark.parametrize(
    "field",
    ["source_schema_version", "source_kind", "source_id", "source_digest"],
)
def test_entity_origin_legacy_rejects_a_missing_source_field(field: str) -> None:
    fields = _legacy_fields()
    del fields[field]
    with pytest.raises(ValidationError, match=f"legacy origin requires {field}"):
        EntityOrigin.model_validate(fields)


def test_entity_origin_legacy_names_every_missing_source_field() -> None:
    fields = _legacy_fields()
    del fields["source_kind"]
    del fields["source_digest"]
    with pytest.raises(ValidationError, match="legacy origin requires source_kind, source_digest"):
        EntityOrigin.model_validate(fields)


def test_entity_origin_legacy_rejects_a_native_mapping_basis() -> None:
    with pytest.raises(ValidationError, match="mapping_basis 'native' belongs to a natively"):
        EntityOrigin.model_validate(_legacy_fields(mapping_basis="native"))


def test_entity_origin_native_rejects_a_source_field() -> None:
    with pytest.raises(ValidationError, match="native origin forbids source_id"):
        EntityOrigin.model_validate({**_NATIVE, "source_id": "legacy-wave-row"})


def test_entity_origin_native_rejects_a_source_urn() -> None:
    with pytest.raises(ValidationError, match="native origin forbids source_urn"):
        EntityOrigin.model_validate({**_NATIVE, "source_urn": TASK_URN})


def test_entity_origin_native_rejects_a_non_native_mapping_basis() -> None:
    with pytest.raises(ValidationError, match="requires mapping_basis 'native', got 'observed'"):
        EntityOrigin.model_validate({**_NATIVE, "mapping_basis": "observed"})


def test_entity_origin_native_rejects_a_non_exact_confidence() -> None:
    with pytest.raises(ValidationError, match="requires confidence 'exact', got 'ambiguous'"):
        EntityOrigin.model_validate({**_NATIVE, "confidence": "ambiguous"})


def test_entity_origin_rejects_an_unknown_mapping_basis() -> None:
    with pytest.raises(ValidationError, match="mapping_basis"):
        EntityOrigin.model_validate(_legacy_fields(mapping_basis="guessed"))


# ---- EntityRef --------------------------------------------------------------


def test_entity_ref_validates_when_the_urn_addresses_its_kind() -> None:
    ref = EntityRef.model_validate({"kind": "task", "urn": TASK_URN})
    assert ref.kind is EntityKind.TASK
    assert ref.urn.kind is EntityKind.TASK


def test_entity_ref_rejects_a_urn_addressing_another_kind() -> None:
    with pytest.raises(
        ValidationError,
        match="reference discriminates task but the URN addresses milestone",
    ):
        EntityRef.model_validate({"kind": "task", "urn": MILESTONE_URN})


def test_entity_ref_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValidationError, match="kind"):
        EntityRef.model_validate({"kind": "epic", "urn": TASK_URN})


# ---- Hold -------------------------------------------------------------------


def test_hold_validates_while_unreleased() -> None:
    hold = Hold.model_validate(_hold_fields())
    assert hold.released_at is None
    assert hold.scope.kind is EntityKind.TRACK


def test_hold_accepts_a_release_at_the_creation_instant() -> None:
    hold = Hold.model_validate(_hold_fields(released_at="2026-09-08T00:00:00Z"))
    assert hold.released_at == hold.created_at


def test_hold_accepts_a_release_after_creation() -> None:
    hold = Hold.model_validate(_hold_fields(released_at="2026-09-08T00:00:01Z"))
    assert hold.released_at is not None
    assert hold.released_at > hold.created_at


def test_hold_rejects_a_release_before_creation() -> None:
    with pytest.raises(ValidationError, match="released_at precedes created_at"):
        Hold.model_validate(_hold_fields(released_at="2026-09-07T23:59:59Z"))


def test_hold_rejects_a_scope_whose_urn_disagrees_with_its_kind() -> None:
    scope = {"kind": "milestone", "urn": TRACK_URN}
    with pytest.raises(ValidationError, match="reference discriminates milestone"):
        Hold.model_validate(_hold_fields(scope=scope))
