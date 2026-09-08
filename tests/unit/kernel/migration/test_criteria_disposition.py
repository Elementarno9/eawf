"""The criteria disposition row: one epoch-2 criterion from a criterion plus its gates.

Four epoch-1 criterion fields convert natively; every other field of the
criterion and of each bound gate is carried on ``legacy_refs``. Nothing
is dropped, and an imported criterion cites the import policy rather than
a source atom it never had.
"""

from __future__ import annotations

import pytest

from eawf.kernel.migration.epoch2.criteria import (
    CRITERION_FIELD_ROUTES,
    DANGLING_GATE_ANNOTATION,
    GATE_FIELD_ROUTES,
    LEGACY_IMPORT_POLICY_REF,
    REVERSE_BOUND_GATE_ANNOTATION,
    FieldDisposition,
    convert_criterion,
)
from eawf.kernel.spec.common import (
    CriterionSpec,
    GateSpec,
    ObserveVerb,
    ProofLocus,
    QualityDimension,
    ResponseClause,
)


def _criterion(*, gate_ids: list[str], waiver_reason: str | None = None) -> CriterionSpec:
    """Build an epoch-1 criterion naming ``gate_ids``."""
    return CriterionSpec(
        id="CR-01",
        text="the importer maps every epoch-1 criterion field onto a declared target",
        kind="attested",
        acceptance_style="binary",
        evidence_kind="attested",
        gate_ids=gate_ids,
        required=True,
        waiver_reason=waiver_reason,
        quality_dimension=QualityDimension.MAINTAINABILITY,
        measurable_signal="the imported criterion carries every source field natively or as legacy",
        response=ResponseClause(
            observe=ObserveVerb.JUDGED,
            object="the imported criterion",
            locus=ProofLocus.HUMAN,
            jury_reason="fixture criterion is satisfied by the focused test assertion",
        ),
    )


def _gate(gate_id: str, *, criterion_id: str = "CR-01") -> GateSpec:
    """Build an epoch-1 gate bound to ``criterion_id``."""
    return GateSpec(
        id=gate_id,
        criterion_id=criterion_id,
        kind="regex_match",
        args={"pattern": "^ok$"},
        policy="block",
        cadence="every-wave",
        required=True,
        timeout_s=30,
    )


def test_convert_criterion_carries_the_legacy_import_policy_ref() -> None:
    """Epoch 1 had no source atoms, so the import policy is the provenance."""
    imported = convert_criterion(criterion=_criterion(gate_ids=["G-01"]), gates=[_gate("G-01")])
    assert imported.policy_ref == LEGACY_IMPORT_POLICY_REF
    assert imported.source_atom_refs == ()


def test_convert_criterion_converts_the_four_native_fields() -> None:
    criterion = _criterion(gate_ids=["G-01"], waiver_reason="waived while the gate is authored")
    imported = convert_criterion(criterion=criterion, gates=[_gate("G-01")])
    assert imported.id == criterion.id
    assert imported.text == criterion.text
    assert imported.gate_ids == ("G-01",)
    assert imported.waiver_reason == "waived while the gate is authored"


def test_convert_criterion_carries_every_unmapped_criterion_field_as_legacy() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=["G-01"]), gates=[_gate("G-01")])
    assert imported.legacy_refs["criterion_kind"] == "attested"
    assert imported.legacy_refs["acceptance_style"] == "binary"
    assert imported.legacy_refs["evidence_kind"] == "attested"
    assert imported.legacy_refs["measurable_signal"].startswith("the imported criterion carries")
    assert imported.legacy_refs["quality_dimension"] == QualityDimension.MAINTAINABILITY.value
    assert imported.legacy_refs["criterion_required"] is True
    assert imported.legacy_refs["response"]["observe"] == ObserveVerb.JUDGED.value
    assert imported.legacy_refs["oracle_tier"] is None


def test_convert_criterion_carries_the_gate_fields_as_legacy() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=["G-01"]), gates=[_gate("G-01")])
    assert imported.legacy_refs["gate_criterion_ids"] == {"G-01": "CR-01"}
    assert imported.legacy_refs["gate_args"] == {"G-01": {"pattern": "^ok$"}}
    assert imported.legacy_refs["gate_cadences"] == {"G-01": "every-wave"}
    assert imported.legacy_refs["gate_kinds"] == {"G-01": "regex_match"}
    assert imported.legacy_refs["gate_policies"] == {"G-01": "block"}
    assert imported.legacy_refs["gate_required"] == {"G-01": True}
    assert imported.legacy_refs["gate_timeouts"] == {"G-01": 30}


def test_convert_criterion_dangling_gate_id_is_carried_not_emitted() -> None:
    """A named gate with no co-resident row must not become a native reference."""
    imported = convert_criterion(
        criterion=_criterion(gate_ids=["G-01", "G-MISSING"]), gates=[_gate("G-01")]
    )
    assert imported.gate_ids == ("G-01",)
    assert imported.legacy_refs["dangling_gate_ids"] == ["G-MISSING"]
    assert DANGLING_GATE_ANNOTATION in imported.annotations


def test_convert_criterion_gate_bound_by_criterion_id_is_not_lost() -> None:
    """The reverse edge counts: a gate naming this criterion joins its gate list."""
    imported = convert_criterion(criterion=_criterion(gate_ids=[]), gates=[_gate("G-02")])
    assert imported.gate_ids == ("G-02",)
    assert REVERSE_BOUND_GATE_ANNOTATION in imported.annotations


def test_convert_criterion_gate_bound_to_another_criterion_is_ignored() -> None:
    imported = convert_criterion(
        criterion=_criterion(gate_ids=["G-01"]),
        gates=[_gate("G-01"), _gate("G-09", criterion_id="CR-99")],
    )
    assert imported.gate_ids == ("G-01",)
    assert imported.legacy_refs["gate_kinds"] == {"G-01": "regex_match"}


def test_convert_criterion_with_no_gates_at_all() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=[]), gates=[])
    assert imported.gate_ids == ()
    assert imported.annotations == ()
    assert imported.legacy_refs["gate_args"] == {}


def test_convert_criterion_all_gate_ids_dangling() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=["G-01", "G-02"]), gates=[])
    assert imported.gate_ids == ()
    assert imported.legacy_refs["dangling_gate_ids"] == ["G-01", "G-02"]


def test_convert_criterion_drops_no_criterion_field() -> None:
    """Every CriterionSpec field is routed, so none can be silently dropped."""
    assert set(CRITERION_FIELD_ROUTES) == set(CriterionSpec.model_fields)


def test_convert_criterion_drops_no_gate_field() -> None:
    assert set(GATE_FIELD_ROUTES) == set(GateSpec.model_fields)


def test_convert_criterion_every_routed_field_reaches_the_imported_record() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=["G-01"]), gates=[_gate("G-01")])
    payload = imported.model_dump(mode="json")
    for routes in (CRITERION_FIELD_ROUTES, GATE_FIELD_ROUTES):
        for field, route in routes.items():
            if route.disposition is FieldDisposition.LEGACY_REF:
                assert route.target_key in imported.legacy_refs, field
            else:
                assert route.target_key in payload, field


def test_convert_criterion_rejects_a_gate_list_that_is_not_iterable() -> None:
    with pytest.raises(TypeError):
        convert_criterion(criterion=_criterion(gate_ids=[]), gates=17)  # type: ignore[arg-type]


def test_imported_criterion_forbids_an_unknown_key() -> None:
    imported = convert_criterion(criterion=_criterion(gate_ids=[]), gates=[])
    with pytest.raises(ValueError):
        type(imported).model_validate(imported.model_dump(mode="json") | {"surprise": 1})
