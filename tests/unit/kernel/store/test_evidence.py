"""Tests for :class:`EvidenceRecord`."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id

pytestmark = pytest.mark.unit


_EV_ID_RE = re.compile(r"^EV-[0-9a-f]{12}$")


def _base_record_kwargs() -> dict[str, object]:
    return {
        "id": mint_evidence_id(),
        "scope_id": "P28-I01-W04",
        "produced_by": "tool",
        "evidence_kind": "deterministic",
        "status": "pass",
        "summary": "pytest gate passed",
        "refs": ["AUD-001"],
        "metrics": {"duration_s": 1.2, "coverage": 0.91, "label": "py"},
        "created_at": datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
    }


def test_mint_evidence_id_format() -> None:
    """Minter returns ``EV-`` plus 12 lowercase hex chars."""
    for _ in range(8):
        ev_id = mint_evidence_id()
        assert _EV_ID_RE.match(ev_id), ev_id


def test_evidence_record_round_trip() -> None:
    """Validates → JSON-dumps → reloads losslessly."""
    record = EvidenceRecord(**_base_record_kwargs())  # type: ignore[arg-type]
    loaded = EvidenceRecord.model_validate_json(record.model_dump_json())
    assert loaded == record
    assert loaded.evidence_kind == "deterministic"
    assert loaded.produced_by == "tool"
    assert loaded.status == "pass"
    assert loaded.metrics == {"duration_s": 1.2, "coverage": 0.91, "label": "py"}


def test_evidence_record_in_envelope_payload() -> None:
    """Envelope-wrapped payload re-validates back into ``EvidenceRecord``."""
    record = EvidenceRecord(**_base_record_kwargs())  # type: ignore[arg-type]
    envelope = Envelope(
        id=record.id,
        kind=StoreKind.EVIDENCE,
        scope_id=record.scope_id,
        created_at=record.created_at,
        summary=record.summary,
        payload=record.model_dump(mode="json"),
    )
    loaded = Envelope.model_validate_json(envelope.model_dump_json())
    assert loaded.kind is StoreKind.EVIDENCE
    payload = EvidenceRecord.model_validate(loaded.payload)
    assert payload == record


def test_evidence_record_registered_in_payload_models() -> None:
    """The registry binds ``StoreKind.EVIDENCE`` to ``EvidenceRecord``."""
    assert PAYLOAD_MODELS[StoreKind.EVIDENCE] is EvidenceRecord


def test_evidence_record_rejects_unknown_field() -> None:
    """``extra='forbid'`` blocks stray keys at validation."""
    kwargs = _base_record_kwargs()
    kwargs["bogus"] = "field"
    with pytest.raises(ValidationError, match="bogus"):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_rejects_unknown_produced_by() -> None:
    kwargs = _base_record_kwargs()
    kwargs["produced_by"] = "outsider"
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_rejects_unknown_evidence_kind() -> None:
    kwargs = _base_record_kwargs()
    kwargs["evidence_kind"] = "telepathy"
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_rejects_unknown_status() -> None:
    kwargs = _base_record_kwargs()
    kwargs["status"] = "maybe"
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_rejects_naive_created_at() -> None:
    """``created_at`` must be timezone-aware UTC."""
    kwargs = _base_record_kwargs()
    kwargs["created_at"] = datetime(2026, 5, 26, 12, 0, 0)  # naive
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_rejects_empty_summary() -> None:
    kwargs = _base_record_kwargs()
    kwargs["summary"] = ""
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_summary_max_length() -> None:
    """Max length is 500 chars."""
    kwargs = _base_record_kwargs()
    kwargs["summary"] = "x" * 501
    with pytest.raises(ValidationError):
        EvidenceRecord(**kwargs)  # type: ignore[arg-type]


def test_evidence_record_metrics_optional() -> None:
    """Metrics defaults to ``None`` and refs defaults to empty list."""
    kwargs = _base_record_kwargs()
    del kwargs["metrics"]
    del kwargs["refs"]
    record = EvidenceRecord(**kwargs)  # type: ignore[arg-type]
    assert record.metrics is None
    assert record.refs == []
