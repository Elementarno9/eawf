"""Measurements re-point at the Task their map key names, and nothing else.

Epoch 1 keyed a measurement by the scope it measures and gave the row a
separate ``EST-`` / ``ACT-`` identifier of its own. Only the first of
those names a subject, so it is the only one the re-point resolves
against. A row whose subject is a phase or an iter has no Task to point
at and keeps the number without claiming it measures one.

The two markers that travel verbatim are the reason a re-pointed corpus
is still a calibration corpus: the quality of a measurement and the flag
saying it must not calibrate are recorded facts, and re-deriving either
from the numbers would quietly promote a disqualified row.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.errors import MigrationFabricationDetectedError
from eawf.kernel.migration.epoch2.measurements import (
    CALIBRATION_EXCLUSION_FIELD,
    MEASUREMENT_COLLECTIONS,
    MEASUREMENT_ID_FIELD,
    QUALITY_MARKER_FIELDS,
    ImportedMeasurement,
    MeasurementImportPlan,
    MeasurementKind,
    map_measurement_row,
    measurement_rule_payload,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from tests.unit.kernel.migration.conftest import build_full_corpus_plan

SOURCE_SCHEMA_VERSION = "1.19"

#: The measurement subjects the full-shape corpus really imports as Tasks.
WAVE_SUBJECT = "P01-I01-W01"
SECOND_WAVE_SUBJECT = "P02-I01-W01"
BACKLOG_SUBJECT = "B003"

#: The subjects that are real scopes and are not Tasks, so no re-point can
#: land on them without inventing one.
ITER_SUBJECTS = ("P01-I01", "P01-I02")
PHASE_SUBJECT = "P01"


def _estimate_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 estimate row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "EST-P01-I01-W01",
        "scope_id": WAVE_SUBJECT,
        "display": "M",
        "confidence": "medium",
        "reference_class": "wave",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _actual_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 actual row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "ACT-P01-I01-W01",
        "scope_id": WAVE_SUBJECT,
        "status": "done",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _map(
    kind: MeasurementKind,
    map_key: str,
    row: dict[str, Any],
    task_ids: frozenset[str] = frozenset({WAVE_SUBJECT}),
) -> ImportedMeasurement:
    """Map one measurement row against a Task population."""
    return map_measurement_row(
        kind=kind,
        map_key=map_key,
        row=row,
        task_ids=task_ids,
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )


def test_measurement_plan_imports_every_source_row(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """The imported population equals the source population, row for row."""
    plan = full_corpus_plan.measurements

    estimates = plan.for_kind(MeasurementKind.ESTIMATE)
    actuals = plan.for_kind(MeasurementKind.ACTUAL)

    assert len(estimates) == 6
    assert len(actuals) == 5
    assert len(plan.measurements) == len(estimates) + len(actuals)


def test_measurement_plan_repoints_every_task_subject_with_zero_orphans(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    task_ids = full_corpus_plan.lifecycle.task_source_ids()
    plan = full_corpus_plan.measurements

    assert plan.orphan_keys == ()
    plan.require_no_orphans()
    for row in plan.measurements:
        if row.map_key in task_ids:
            assert row.task_ref == row.map_key
            assert not row.is_immutable_legacy_record
        else:
            assert row.task_ref is None


def test_measurement_plan_repoints_on_the_map_key_not_the_row_id(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """The ``EST-`` id names the measurement; only the map key names a subject."""
    estimate = full_corpus_plan.measurements.measurement_for(
        kind=MeasurementKind.ESTIMATE, map_key=WAVE_SUBJECT
    )

    assert estimate.map_key == WAVE_SUBJECT
    assert estimate.source_row_id == f"EST-{WAVE_SUBJECT}"
    assert estimate.task_ref == WAVE_SUBJECT
    assert estimate.task_ref != estimate.source_row_id


def test_measurement_plan_repoints_a_backlog_subject_at_its_task(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """A backlog row is a Task too, so a measurement of one re-points."""
    estimate = full_corpus_plan.measurements.measurement_for(
        kind=MeasurementKind.ESTIMATE, map_key=BACKLOG_SUBJECT
    )
    actual = full_corpus_plan.measurements.measurement_for(
        kind=MeasurementKind.ACTUAL, map_key=BACKLOG_SUBJECT
    )

    assert estimate.task_ref == BACKLOG_SUBJECT
    assert actual.task_ref == BACKLOG_SUBJECT


def test_measurement_plan_leaves_a_subject_with_no_task_a_legacy_record(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """An iter and a phase are real scopes and are not Tasks."""
    plan = full_corpus_plan.measurements

    assert set(plan.legacy_keys) == {PHASE_SUBJECT, *ITER_SUBJECTS}
    for map_key, kind in (
        (PHASE_SUBJECT, MeasurementKind.ESTIMATE),
        (ITER_SUBJECTS[0], MeasurementKind.ESTIMATE),
        (ITER_SUBJECTS[1], MeasurementKind.ACTUAL),
    ):
        row = plan.measurement_for(kind=kind, map_key=map_key)
        assert row.is_immutable_legacy_record
        assert row.task_ref is None
        assert row.origin.confidence == "supported"


def test_measurement_plan_carries_the_quality_marker_verbatim(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """Each kind's marker is read from its own field and copied, not mapped."""
    plan = full_corpus_plan.measurements

    for row in plan.for_kind(MeasurementKind.ESTIMATE):
        assert row.quality_marker == row.payload[QUALITY_MARKER_FIELDS[MeasurementKind.ESTIMATE]]
    for row in plan.for_kind(MeasurementKind.ACTUAL):
        assert row.quality_marker == row.payload[QUALITY_MARKER_FIELDS[MeasurementKind.ACTUAL]]

    assert {row.quality_marker for row in plan.for_kind(MeasurementKind.ESTIMATE)} == {
        "high",
        "medium",
        "low",
    }
    assert {row.quality_marker for row in plan.for_kind(MeasurementKind.ACTUAL)} == {
        "recorded",
        "interrupted",
        "done",
        "abandoned",
    }


def test_measurement_plan_carries_the_exclusion_flag_verbatim(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    """Marked-excluded, marked-eligible and unmarked are three distinct states."""
    plan = full_corpus_plan.measurements

    excluded = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=SECOND_WAVE_SUBJECT)
    eligible = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=BACKLOG_SUBJECT)
    unmarked = plan.measurement_for(kind=MeasurementKind.ACTUAL, map_key=WAVE_SUBJECT)

    assert excluded.calibration_excluded is True
    assert eligible.calibration_excluded is False
    assert unmarked.calibration_excluded is None
    assert CALIBRATION_EXCLUSION_FIELD not in unmarked.payload


def test_measurement_plan_preserves_the_source_row(
    full_corpus_plan: CorpusImportPlan,
) -> None:
    for row in full_corpus_plan.measurements.measurements:
        assert row.payload[MEASUREMENT_ID_FIELD] == row.source_row_id
        assert row.origin.kind == "legacy"
        assert row.origin.source_kind == MEASUREMENT_COLLECTIONS[row.kind]
        assert row.origin.source_id == row.map_key
        assert row.origin.source_digest is not None


def test_measurement_plan_build_is_deterministic() -> None:
    assert build_full_corpus_plan().measurements == build_full_corpus_plan().measurements


def test_map_measurement_row_of_a_subject_with_no_task_defers_the_reference() -> None:
    row = _map(MeasurementKind.ESTIMATE, "P01", _estimate_row(scope_id="P01"))

    assert row.task_ref is None
    assert row.is_immutable_legacy_record


def test_map_measurement_row_reads_an_unrecorded_quality_marker_as_none() -> None:
    """An absent marker is unknown quality, never a defaulted one."""
    estimate = _map(MeasurementKind.ESTIMATE, WAVE_SUBJECT, _estimate_row(confidence=None))
    actual = _map(MeasurementKind.ACTUAL, WAVE_SUBJECT, _actual_row(status=""))

    assert estimate.quality_marker is None
    assert actual.quality_marker is None


def test_map_measurement_row_reads_a_non_boolean_exclusion_flag_as_unrecorded() -> None:
    row = _map(MeasurementKind.ACTUAL, WAVE_SUBJECT, _actual_row(calibration_excluded="yes"))

    assert row.calibration_excluded is None


def test_map_measurement_row_reads_an_unusable_row_id_as_absent() -> None:
    row = _map(MeasurementKind.ACTUAL, WAVE_SUBJECT, _actual_row(id=""))

    assert row.source_row_id is None


def test_map_measurement_row_rejects_an_empty_map_key() -> None:
    with pytest.raises(ValidationError):
        _map(MeasurementKind.ESTIMATE, "", _estimate_row())


def test_map_measurement_row_rejects_a_row_that_cannot_be_digested() -> None:
    """A row holding a value JSON cannot encode has no reproducible digest."""
    with pytest.raises(TypeError):
        _map(MeasurementKind.ESTIMATE, WAVE_SUBJECT, _estimate_row(display={1, 2}))


def test_measurement_import_plan_build_over_an_empty_document() -> None:
    plan = MeasurementImportPlan.build(
        document={}, task_ids=frozenset(), source_schema_version=SOURCE_SCHEMA_VERSION
    )

    assert plan.measurements == ()
    assert plan.orphan_keys == ()
    assert plan.legacy_keys == ()
    plan.require_no_orphans()


def test_measurement_import_plan_build_over_a_single_row() -> None:
    plan = MeasurementImportPlan.build(
        document={"estimates": {WAVE_SUBJECT: _estimate_row()}, "actuals": None},
        task_ids=frozenset({WAVE_SUBJECT}),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert len(plan.measurements) == 1
    assert plan.for_kind(MeasurementKind.ACTUAL) == ()
    assert plan.measurements[0].task_ref == WAVE_SUBJECT


def test_measurement_import_plan_build_skips_a_row_that_is_not_an_object() -> None:
    plan = MeasurementImportPlan.build(
        document={"estimates": {WAVE_SUBJECT: "not a row"}},
        task_ids=frozenset({WAVE_SUBJECT}),
        source_schema_version=SOURCE_SCHEMA_VERSION,
    )

    assert plan.measurements == ()


def test_measurement_import_plan_measurement_for_rejects_an_unknown_key() -> None:
    plan = MeasurementImportPlan.build(
        document={}, task_ids=frozenset(), source_schema_version=SOURCE_SCHEMA_VERSION
    )

    with pytest.raises(KeyError, match="estimates"):
        plan.measurement_for(kind=MeasurementKind.ESTIMATE, map_key=WAVE_SUBJECT)


def test_measurement_import_plan_require_no_orphans_refuses_an_unbacked_repoint() -> None:
    """A re-point at a Task the import never wrote is a fabricated subject."""
    orphan = _map(MeasurementKind.ESTIMATE, WAVE_SUBJECT, _estimate_row())
    plan = MeasurementImportPlan(
        measurements=(orphan,), orphan_keys=(WAVE_SUBJECT,), legacy_keys=()
    )

    with pytest.raises(MigrationFabricationDetectedError, match="never"):
        plan.require_no_orphans()


def test_measurement_rule_payload_names_both_markers_and_the_key_it_uses() -> None:
    payload = measurement_rule_payload()

    assert payload["keyed_on"] == "map_key"
    assert payload["never_keyed_on"] == MEASUREMENT_ID_FIELD
    assert payload["quality_markers"] == {
        "estimate": "confidence",
        "actual": "status",
    }
    assert payload["exclusion_flag"] == CALIBRATION_EXCLUSION_FIELD
    assert payload["unbacked_subject_imports_as"] == "immutable_legacy_record"
