"""Schema tests for :class:`WaveSpecBody` — the wave-spec body parse-target.

The wave-spec markdown body carries a fenced structured block that
encodes a criteria block + a gate block. This module pins the typed
parse-target that block validates against (wave P29-I12-W03). Coverage:

* a well-formed body parses to the right :class:`CriterionSpec` /
  :class:`GateSpec` rows (the happy path);
* boundary: an empty criteria list (and an all-empty body) validates;
* error path: an unknown top-level key fails ``extra="forbid"``;
* error path: an unknown field on a criterion / gate row fails;
* error path: a missing / sub-floor ``measurable_signal`` fails the
  inherited :class:`CriterionSpec` 20-char floor;
* referential integrity: a gate pointing at an absent criterion, or a
  criterion pointing at an absent gate, fails the cross-list validator;
* the :meth:`WaveSpecBody.from_mapping` loader round-trips the same.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.spec.wave_body import WAVE_BODY_FENCE, WaveSpecBody

# A measurable_signal string of exactly 20 characters (the min_length floor).
_SIGNAL_20 = "x" * 20
# One short of the floor.
_SIGNAL_19 = "x" * 19


def _criterion_row(*, gate_ids: list[str] | None = None) -> dict[str, Any]:
    """One well-formed criterion mapping for a wave-spec body block."""
    return {
        "id": "CR-01",
        "text": "render the close-readiness header in the evidence mode",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "interaction_capability",
        "measurable_signal": "the snapshot test for the evidence header passes",
        "gate_ids": gate_ids if gate_ids is not None else [],
    }


def _gate_row() -> dict[str, Any]:
    """One well-formed gate mapping (schema_validate — no argv policy)."""
    return {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": "schema_validate",
        "args": {"model": "CloseReadiness"},
        "policy": "block",
        "cadence": "every-wave",
    }


# --------------------------------------------------------------------------- #
# Happy path — a well-formed body parses to the right rows.
# --------------------------------------------------------------------------- #
def test_wave_body_full_parses_to_typed_rows() -> None:
    """A criteria block + gate block parse to CriterionSpec / GateSpec rows."""
    body = WaveSpecBody.model_validate(
        {
            "criteria": [_criterion_row(gate_ids=["G-01"])],
            "gates": [_gate_row()],
        }
    )
    assert len(body.criteria) == 1
    assert len(body.gates) == 1

    criterion = body.criteria[0]
    gate = body.gates[0]
    assert isinstance(criterion, CriterionSpec)
    assert isinstance(gate, GateSpec)
    assert criterion.id == "CR-01"
    assert criterion.evidence_kind == "deterministic"
    assert criterion.gate_ids == ["G-01"]
    assert gate.id == "G-01"
    assert gate.criterion_id == "CR-01"
    assert gate.kind == "schema_validate"


def test_wave_body_from_mapping_round_trips() -> None:
    """The from_mapping loader yields a validated body identical to a direct call."""
    data = {
        "criteria": [_criterion_row(gate_ids=["G-01"])],
        "gates": [_gate_row()],
    }
    body = WaveSpecBody.from_mapping(data)
    assert body == WaveSpecBody.model_validate(data)
    assert body.criteria[0].id == "CR-01"


def test_wave_body_fence_label_is_canonical() -> None:
    """The fence label the W04 extractor keys on is the documented constant."""
    assert WAVE_BODY_FENCE == "eawf-wave-body"


# --------------------------------------------------------------------------- #
# Boundary — empty lists validate.
# --------------------------------------------------------------------------- #
def test_wave_body_empty_criteria_list_validates() -> None:
    """An empty criteria list is a valid (advisory-wave) body."""
    body = WaveSpecBody.model_validate({"criteria": [], "gates": []})
    assert body.criteria == []
    assert body.gates == []


def test_wave_body_default_empty_when_keys_omitted() -> None:
    """Both lists default to empty when the keys are omitted entirely."""
    body = WaveSpecBody.model_validate({})
    assert body.criteria == []
    assert body.gates == []


def test_wave_body_criteria_without_gates_validates() -> None:
    """An attested criterion with no gate ref needs no gate row."""
    body = WaveSpecBody.model_validate({"criteria": [_criterion_row()], "gates": []})
    assert body.criteria[0].gate_ids == []
    assert body.gates == []


# --------------------------------------------------------------------------- #
# Error path — extra="forbid" at the top level and on rows.
# --------------------------------------------------------------------------- #
def test_wave_body_unknown_top_level_key_raises() -> None:
    """An unknown top-level key fails the inherited extra='forbid' config."""
    with pytest.raises(ValidationError) as exc:
        WaveSpecBody.model_validate({"criteria": [], "gates": [], "bogus_field": True})
    assert "extra" in str(exc.value).lower() or "bogus_field" in str(exc.value)


def test_wave_body_unknown_criterion_field_raises() -> None:
    """An unknown field on a criterion row fails extra='forbid'."""
    row = _criterion_row()
    row["surprise"] = "nope"
    with pytest.raises(ValidationError):
        WaveSpecBody.model_validate({"criteria": [row], "gates": []})


def test_wave_body_unknown_gate_field_raises() -> None:
    """An unknown field on a gate row fails extra='forbid'."""
    gate = _gate_row()
    gate["surprise"] = "nope"
    with pytest.raises(ValidationError):
        WaveSpecBody.model_validate(
            {"criteria": [_criterion_row(gate_ids=["G-01"])], "gates": [gate]}
        )


# --------------------------------------------------------------------------- #
# Error path — measurable_signal floor carries through.
# --------------------------------------------------------------------------- #
def test_wave_body_missing_measurable_signal_raises() -> None:
    """A criterion with no measurable_signal fails the required-field check."""
    row = _criterion_row()
    del row["measurable_signal"]
    with pytest.raises(ValidationError) as exc:
        WaveSpecBody.model_validate({"criteria": [row], "gates": []})
    assert "measurable_signal" in str(exc.value)


def test_wave_body_short_measurable_signal_raises() -> None:
    """A measurable_signal under the 20-char floor fails the min_length bound."""
    row = _criterion_row()
    row["measurable_signal"] = _SIGNAL_19
    with pytest.raises(ValidationError) as exc:
        WaveSpecBody.model_validate({"criteria": [row], "gates": []})
    assert "measurable_signal" in str(exc.value)


def test_wave_body_floor_measurable_signal_validates() -> None:
    """A measurable_signal of exactly 20 chars clears the floor."""
    row = _criterion_row()
    row["measurable_signal"] = _SIGNAL_20
    body = WaveSpecBody.model_validate({"criteria": [row], "gates": []})
    assert body.criteria[0].measurable_signal == _SIGNAL_20


# --------------------------------------------------------------------------- #
# Error path — cross-list referential integrity.
# --------------------------------------------------------------------------- #
def test_wave_body_gate_referencing_absent_criterion_raises() -> None:
    """A gate.criterion_id naming no present criterion fails the validator."""
    gate = _gate_row()
    gate["criterion_id"] = "CR-99"
    with pytest.raises(ValidationError) as exc:
        WaveSpecBody.model_validate({"criteria": [_criterion_row()], "gates": [gate]})
    assert "unknown criterion" in str(exc.value)


def test_wave_body_criterion_referencing_absent_gate_raises() -> None:
    """A criterion.gate_ids naming no present gate fails the validator."""
    with pytest.raises(ValidationError) as exc:
        WaveSpecBody.model_validate({"criteria": [_criterion_row(gate_ids=["G-99"])], "gates": []})
    assert "unknown gate" in str(exc.value)
