"""The four lifecycle collections, mapped onto epoch-2 records.

Every phase, iter, wave and backlog row of the full-shape snapshot yields
exactly one target. Each target carries a legacy origin naming the row it
came from and the digest of that row's bytes, the closed status map
decides its status, and the two Task fields epoch 1 never recorded arrive
annotated rather than silently filled.

Archival is the sharpest case: the archived phase reaches ``CANCELLED``
and keeps its acceptance proof unfilled, because the source recorded that
the phase stopped and never that anyone accepted it.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.migration.epoch2.criteria import ImportedCriterion
from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.lifecycle import (
    LIFECYCLE_FIELD_ROUTES,
    MILESTONE_ACCEPTANCE_FIELDS,
    MILESTONE_UNRECORDED_FIELDS,
    DeferralReason,
    LifecycleFieldDisposition,
    LifecycleSourceIndex,
    LifecycleTarget,
    map_phase_row,
    map_wave_row,
)
from eawf.kernel.migration.epoch2.plan import LifecycleImportPlan
from eawf.kernel.migration.epoch2.rules import rule_digest
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.migration.epoch2.status_map import (
    INTENT_FROM_TITLE_ANNOTATION,
    PRIORITY_DEFAULT_ANNOTATION,
    SourceLifecycle,
)
from eawf.kernel.state.models import BacklogItem, Iter, Phase, Wave
from tests.unit.kernel.migration.conftest import ALLOWLIST_PATH, EPOCH1_FULL_SNAPSHOT

SOURCE_SCHEMA_VERSION = "1.19"

#: Every lifecycle row of the full-shape snapshot, in the order the plan
#: emits them: collection by collection, then by source id.
EXPECTED_SOURCE_IDS = (
    "P01",
    "P02",
    "P03",
    "P01-I01",
    "P01-I02",
    "P02-I01",
    "P01-I01-W01",
    "P01-I01-W02",
    "P02-I01-W01",
    "B001",
    "B002",
    "B003",
)

EXPECTED_STATUSES = {
    "P01": "COMPLETED",
    "P02": "COMPLETED",
    "P03": "CANCELLED",
    "P01-I01": "COMPLETED",
    "P01-I02": "COMPLETED",
    "P02-I01": "COMPLETED",
    "P01-I01-W01": "COMPLETED",
    "P01-I01-W02": "COMPLETED",
    "P02-I01-W01": "COMPLETED",
    "B001": "DRAFT",
    "B002": "DRAFT",
    "B003": "DROPPED",
}

EXPECTED_TARGETS = {
    SourceLifecycle.PHASE: LifecycleTarget.MILESTONE,
    SourceLifecycle.ITER: LifecycleTarget.BATCH,
    SourceLifecycle.WAVE: LifecycleTarget.TASK,
    SourceLifecycle.BACKLOG: LifecycleTarget.TASK,
}


def _index() -> LifecycleSourceIndex:
    """A source index holding two tracks and one session."""
    return LifecycleSourceIndex.build(
        {
            "schema_version": SOURCE_SCHEMA_VERSION,
            "tracks": {"TRK-CORE": {"id": "TRK-CORE"}, "TRK-PLAT": {"id": "TRK-PLAT"}},
            "agent_sessions": {"S001": {"id": "S001"}},
        },
        report_rows=(),
    )


def _phase_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 phase row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "P09",
        "scope_id": "P09",
        "status": "closed",
        "title": "Deliver P09",
        "opened_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def _wave_row(**overrides: Any) -> dict[str, Any]:
    """One epoch-1 wave row, overridable field by field."""
    row: dict[str, Any] = {
        "id": "P09-I01-W01",
        "iter_id": "P09-I01",
        "status": "pending",
        "title": "Land P09-I01-W01",
        "opened_at": "2026-01-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_lifecycle_import_plan_build_maps_every_lifecycle_row_once(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    source_ids = tuple(record.origin.source_id for record in full_lifecycle_plan.records)

    assert source_ids == EXPECTED_SOURCE_IDS
    assert len(source_ids) == len(set(source_ids))


def test_lifecycle_import_plan_build_gives_every_record_a_legacy_origin(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    for record in full_lifecycle_plan.records:
        assert record.origin.kind == "legacy"
        assert record.origin.source_schema_version == SOURCE_SCHEMA_VERSION
        assert record.origin.source_kind == record.source_collection
        assert record.origin.mapping_basis == "mechanical"
        assert record.origin.source_digest is not None
        assert record.origin.source_digest.startswith("sha256:")


def test_lifecycle_import_plan_build_digests_the_source_row_it_read(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    """The digest pins the row's bytes, so a re-run over an edited row differs."""
    record = full_lifecycle_plan.record_for("P03")
    row = {
        "id": "P03",
        "opened_at": "2026-01-01T00:00:00Z",
        "scope_id": "P03",
        "status": "archived",
        "title": "Archive P03",
    }

    assert record.origin.source_digest == f"sha256:{rule_digest(row)}"


def test_lifecycle_import_plan_build_applies_the_closed_status_map(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    mapped = {
        record.origin.source_id: record.target_status for record in full_lifecycle_plan.records
    }

    assert mapped == EXPECTED_STATUSES


def test_lifecycle_import_plan_build_maps_the_archived_phase_to_cancelled(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    """Archival records that the phase stopped, never that it was accepted."""
    archived = full_lifecycle_plan.record_for("P03")

    assert archived.target_status == "CANCELLED"
    assert archived.record["status"] == "CANCELLED"
    for field in MILESTONE_ACCEPTANCE_FIELDS:
        assert field not in archived.record


def test_lifecycle_import_plan_build_targets_each_collection_at_one_kind(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    for lifecycle, target in EXPECTED_TARGETS.items():
        records = full_lifecycle_plan.for_lifecycle(lifecycle)
        assert records
        assert {record.target for record in records} == {target}


def test_lifecycle_import_plan_build_annotates_the_defaulted_priority_and_intent(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    """Epoch 1 gave a wave neither field, so both fills carry their annotation."""
    task = full_lifecycle_plan.record_for("P01-I01-W01")

    assert task.record["priority"] == "P2"
    assert task.record["intent"] == "Land P01-I01-W01"
    assert PRIORITY_DEFAULT_ANNOTATION in task.annotations
    assert INTENT_FROM_TITLE_ANNOTATION in task.annotations
    assert task.ledger_annotations == task.annotations


def test_lifecycle_import_plan_build_keeps_a_recorded_backlog_priority_unannotated(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    dropped = full_lifecycle_plan.record_for("B003")

    assert dropped.record["priority"] == "P1"
    assert PRIORITY_DEFAULT_ANNOTATION not in dropped.annotations


def test_lifecycle_import_plan_build_carries_the_backlog_classifier_verdict(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    """The closed row was classified once, by the backlog plan, not twice."""
    dropped = full_lifecycle_plan.record_for("B003")

    assert dropped.resolution is not None
    assert dropped.resolution.delivered_by_wave_id == "P01-I01-W01"
    assert dropped.target_status == "DROPPED"


def test_lifecycle_import_plan_build_preserves_unmapped_source_fields(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    """A DeliveryBatch has no title, so the iter's title survives as a legacy ref."""
    batch = full_lifecycle_plan.for_lifecycle(SourceLifecycle.ITER)[0]

    assert batch.legacy_refs["title"] == "Close P01-I01"
    assert batch.legacy_refs["audit_id"] == "A001"
    assert "title" not in batch.record


def test_lifecycle_import_plan_build_is_deterministic() -> None:
    """Two passes over one snapshot agree row for row."""
    first = LifecycleImportPlan.build(
        snapshot=SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT), allowlist_path=ALLOWLIST_PATH
    )
    second = LifecycleImportPlan.build(
        snapshot=SourceSnapshot.read(EPOCH1_FULL_SNAPSHOT), allowlist_path=ALLOWLIST_PATH
    )

    assert first == second


def test_lifecycle_import_plan_record_for_rejects_an_unimported_id(
    full_lifecycle_plan: LifecycleImportPlan,
) -> None:
    with pytest.raises(KeyError):
        full_lifecycle_plan.record_for("P99")


def test_lifecycle_field_routes_are_total_over_the_source_models() -> None:
    """A field added to an epoch-1 model has to be routed before it can import."""
    declared = {
        SourceLifecycle.PHASE: set(Phase.model_fields),
        SourceLifecycle.ITER: set(Iter.model_fields),
        SourceLifecycle.WAVE: set(Wave.model_fields),
        SourceLifecycle.BACKLOG: set(BacklogItem.model_fields),
    }

    for lifecycle, fields in declared.items():
        assert set(LIFECYCLE_FIELD_ROUTES[lifecycle]) == fields


def test_lifecycle_field_routes_give_every_collection_one_identity_field() -> None:
    for lifecycle, routes in LIFECYCLE_FIELD_ROUTES.items():
        identities = [
            field
            for field, route in routes.items()
            if route.disposition is LifecycleFieldDisposition.IDENTITY
        ]
        assert identities == ["id"], lifecycle


def test_map_phase_row_assigns_a_track_the_source_really_holds() -> None:
    record = map_phase_row(source_id="P09", row=_phase_row(track_id="TRK-CORE"), index=_index())

    assert record.record["primary_track_ref"] == "TRK-CORE"
    assert "primary_track_ref" not in record.deferred_field_names()
    assert record.origin.confidence == "supported"


def test_map_phase_row_leaves_an_unresolved_track_unassigned() -> None:
    record = map_phase_row(source_id="P09", row=_phase_row(track_id="TRK-GONE"), index=_index())
    deferral = next(
        field for field in record.deferred_fields if field.target_field == "primary_track_ref"
    )

    assert "primary_track_ref" not in record.record
    assert deferral.reason is DeferralReason.UNRESOLVED_SOURCE_REFERENCE
    assert record.legacy_refs["track_id"] == "TRK-GONE"


def test_map_phase_row_defers_every_milestone_field_epoch1_never_recorded() -> None:
    record = map_phase_row(source_id="P09", row=_phase_row(), index=_index())

    assert set(MILESTONE_UNRECORDED_FIELDS) <= set(record.deferred_field_names())
    for field in MILESTONE_UNRECORDED_FIELDS:
        assert field not in record.record


def test_map_phase_row_unknown_status_raises_count_mismatch() -> None:
    with pytest.raises(
        MigrationCountMismatchError, match="outside the closed status map"
    ) as raised:
        map_phase_row(source_id="P09", row=_phase_row(status="paused"), index=_index())

    assert raised.value.code == "migration_count_mismatch"


def test_map_phase_row_empty_status_raises_count_mismatch() -> None:
    with pytest.raises(MigrationCountMismatchError):
        map_phase_row(source_id="P09", row=_phase_row(status=""), index=_index())


def test_map_wave_row_takes_the_intent_from_the_source_brief_outcome() -> None:
    row = _wave_row(intent={"problem": "p", "desired_outcome": "the importer maps the wave"})
    record = map_wave_row(source_id=row["id"], row=row, criteria=(), index=_index())

    assert record.record["intent"] == "the importer maps the wave"
    assert INTENT_FROM_TITLE_ANNOTATION not in record.annotations
    assert record.legacy_refs["intent"] == row["intent"]


def test_map_wave_row_without_a_title_or_intent_raises_count_mismatch() -> None:
    row = _wave_row()
    del row["title"]

    with pytest.raises(MigrationCountMismatchError, match="carries no title"):
        map_wave_row(source_id=row["id"], row=row, criteria=(), index=_index())


def test_map_wave_row_defers_criteria_only_when_the_source_recorded_none() -> None:
    """A planned Task needs a contract; the importer names the gap, never fills it."""
    imported = ImportedCriterion(
        id="CR-01",
        text="the wave carries one recorded criterion",
        policy_ref="legacy-import",
        source_atom_refs=(),
        gate_ids=(),
        waiver_reason=None,
        legacy_refs={},
        annotations=(),
    )
    without = map_wave_row(
        source_id="P09-I01-W01", row=_wave_row(status="pending"), criteria=(), index=_index()
    )
    with_criteria = map_wave_row(
        source_id="P09-I01-W01",
        row=_wave_row(status="pending"),
        criteria=(imported,),
        index=_index(),
    )

    assert "criteria" in without.deferred_field_names()
    assert "criteria" not in with_criteria.deferred_field_names()
    assert with_criteria.criteria == (imported,)


def test_lifecycle_source_index_build_without_a_schema_version_raises() -> None:
    with pytest.raises(MigrationCountMismatchError, match="schema_version"):
        LifecycleSourceIndex.build({"tracks": None}, report_rows=())


def test_lifecycle_source_index_build_reads_a_document_with_no_tracks() -> None:
    index = LifecycleSourceIndex.build({"schema_version": "1.19", "tracks": None}, report_rows=())

    assert index.track_ids == ()
    assert index.session_ids == frozenset()
