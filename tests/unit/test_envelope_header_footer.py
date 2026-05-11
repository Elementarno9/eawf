"""Unit tests for the typed :class:`EnvelopeHeader` and :class:`EnvelopeFooter`.

Phase 4 W01 narrowed both from ``dict[str, Any]`` to dedicated Pydantic
models with ``extra="forbid"``. These tests pin:

- the required-field set (skill, scope_id, session, started_at,
  finished_at, status);
- the frozen literal enums (skill, status, instrument_probe values);
- the ``extra="forbid"`` rejection on unknown keys for both header and
  footer; and
- the optional/defaulted fields on the footer (every list defaults to
  empty; ``repair_commands`` is ``None`` by default).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.render.envelope import EnvelopeFooter, EnvelopeHeader, EnvelopeWarning


def _base_header(**overrides: object) -> dict[str, object]:
    """Helper mirroring the pattern from ``tests/unit/test_envelope.py:14``."""
    defaults: dict[str, object] = {
        "skill": "/research",
        "scope_id": "urn:eawf:v1:state:QR/P00",
        "session": "urn:eawf:v1:store:QR/sessions/SES-1",
        "started_at": datetime(2026, 5, 9, 0, 0, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 5, 9, 0, 0, 1, tzinfo=UTC),
        "status": "ok",
        "instrument_probe": {"git": "ok"},
    }
    defaults.update(overrides)
    return defaults


def test_header_round_trip_json() -> None:
    """Pydantic v2 model_dump_json/model_validate_json round-trips."""
    header = EnvelopeHeader(**_base_header())  # type: ignore[arg-type]
    parsed = EnvelopeHeader.model_validate_json(header.model_dump_json())
    assert parsed == header


def test_header_rejects_unknown_skill_literal() -> None:
    """``header.skill`` is a frozen Literal; arbitrary names are rejected."""
    with pytest.raises(ValidationError, match="Input should be"):
        EnvelopeHeader(**_base_header(skill="research-spike"))  # type: ignore[arg-type]


def test_header_rejects_unknown_status_literal() -> None:
    """``header.status`` is one of the five frozen statuses."""
    with pytest.raises(ValidationError, match="Input should be"):
        EnvelopeHeader(**_base_header(status="cancelled"))  # type: ignore[arg-type]


def test_header_rejects_unknown_instrument_status() -> None:
    """``instrument_probe`` values are one of ok/missing/degraded."""
    with pytest.raises(ValidationError, match="Input should be"):
        EnvelopeHeader(**_base_header(instrument_probe={"git": "broken"}))  # type: ignore[arg-type]


def test_header_rejects_extra_field() -> None:
    """``extra='forbid'`` blocks unknown header keys."""
    data = _base_header()
    data["unexpected"] = "oops"
    with pytest.raises(ValidationError, match="Extra inputs"):
        EnvelopeHeader.model_validate(data)


def test_header_requires_started_and_finished() -> None:
    """The two timestamp fields are required."""
    data = _base_header()
    del data["started_at"]
    with pytest.raises(ValidationError, match="started_at"):
        EnvelopeHeader.model_validate(data)


def test_header_instrument_probe_defaults_to_empty_dict() -> None:
    """``instrument_probe`` defaults to an empty mapping."""
    data = _base_header()
    del data["instrument_probe"]
    header = EnvelopeHeader.model_validate(data)
    assert header.instrument_probe == {}


def test_footer_defaults_every_list_to_empty() -> None:
    """All six list fields default to an empty list."""
    footer = EnvelopeFooter()
    assert footer.persisted_artifacts == []
    assert footer.persisted_store_records == []
    assert footer.state_mutations == []
    assert footer.evidence_refs == []
    assert footer.next_valid_actions == []
    assert footer.warnings == []
    assert footer.repair_commands is None


def test_footer_rejects_extra_field() -> None:
    """``extra='forbid'`` blocks unknown footer keys."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        EnvelopeFooter.model_validate({"unexpected": "oops"})


def test_footer_warnings_typed_round_trip() -> None:
    """Warnings dict ⇆ :class:`EnvelopeWarning` model coercion via model_validate."""
    footer = EnvelopeFooter.model_validate(
        {
            "warnings": [
                {"code": "instrument_missing", "detail": "gh not installed"},
                {"code": "hook_blocked", "detail": "pre-commit failed"},
            ]
        }
    )
    assert len(footer.warnings) == 2
    assert all(isinstance(w, EnvelopeWarning) for w in footer.warnings)
    assert footer.warnings[0].code == "instrument_missing"
    assert footer.warnings[0].detail == "gh not installed"


def test_footer_warning_rejects_extra_field() -> None:
    """``EnvelopeWarning.extra='forbid'`` blocks unknown warning keys."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        EnvelopeFooter.model_validate({"warnings": [{"code": "x", "detail": "y", "level": "high"}]})


def test_footer_repair_commands_optional_list() -> None:
    """``repair_commands`` accepts ``None`` or ``list[str]``; rejects scalars."""
    ok = EnvelopeFooter(repair_commands=["eawf install gh"])
    assert ok.repair_commands == ["eawf install gh"]

    with pytest.raises(ValidationError, match="should be a valid list"):
        EnvelopeFooter.model_validate({"repair_commands": "eawf install gh"})


def test_header_finished_at_before_started_at_rejected() -> None:
    """The model rejects ``finished_at < started_at``.

    Originally pinned the permissive behaviour (engine-only enforcement);
    Phase 4 W01 review flipped this to a model-level validator so
    hand-built envelopes (e.g. CLI hook handlers) cannot violate the
    contract.
    """
    started = datetime(2026, 5, 9, 0, 0, 5, tzinfo=UTC)
    finished = datetime(2026, 5, 9, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="finished_at must be >= started_at"):
        EnvelopeHeader(**_base_header(started_at=started, finished_at=finished))  # type: ignore[arg-type]
