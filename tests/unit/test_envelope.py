"""Tests for store.envelope.Envelope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope


def _base_envelope(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "id": "env-001",
        "kind": StoreKind.RESEARCH,
        "scope_id": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "summary": "A short summary",
        "payload": {"topic": "test", "findings": [], "sources": []},
    }
    defaults.update(overrides)
    return defaults


def test_envelope_round_trip_json() -> None:
    env = Envelope(**_base_envelope())  # type: ignore[arg-type]
    json_str = env.model_dump_json()
    env2 = Envelope.model_validate_json(json_str)
    assert env == env2


def test_envelope_summary_max_length_rejected() -> None:
    with pytest.raises(ValidationError):
        Envelope(**_base_envelope(summary="x" * 501))  # type: ignore[arg-type]


def test_envelope_unknown_kind_rejected() -> None:
    data = dict(_base_envelope())
    data["kind"] = "not_a_real_kind"
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_envelope_extra_field_rejected() -> None:
    data = dict(_base_envelope())
    data["extra_bogus_field"] = "should fail"
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_envelope_default_blob_refs_and_artifact_ids() -> None:
    env = Envelope(**_base_envelope())  # type: ignore[arg-type]
    assert env.blob_refs == []
    assert env.artifact_ids == []


def test_envelope_schema_version_defaults_to_1_0() -> None:
    env = Envelope(**_base_envelope())  # type: ignore[arg-type]
    assert env.schema_version == "1.0"


def test_envelope_summary_exactly_500_chars_accepted() -> None:
    env = Envelope(**_base_envelope(summary="y" * 500))  # type: ignore[arg-type]
    assert len(env.summary) == 500


def test_envelope_updated_at_defaults_none() -> None:
    env = Envelope(**_base_envelope())  # type: ignore[arg-type]
    assert env.updated_at is None


def test_envelope_scope_id_none_accepted() -> None:
    env = Envelope(**_base_envelope(scope_id=None))  # type: ignore[arg-type]
    assert env.scope_id is None


def test_envelope_event_kind_with_non_null_updated_at_raises() -> None:
    data = dict(_base_envelope())
    data["kind"] = StoreKind.EVENT
    data["updated_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        Envelope.model_validate(data)


def test_envelope_event_kind_with_null_updated_at_accepted() -> None:
    data = dict(_base_envelope())
    data["kind"] = StoreKind.EVENT
    env = Envelope.model_validate(data)
    assert env.updated_at is None


def test_envelope_empty_id_raises() -> None:
    with pytest.raises(ValidationError):
        Envelope(**_base_envelope(id=""))  # type: ignore[arg-type]
