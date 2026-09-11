"""The manifest refuses, at load, every claim it cannot re-derive.

The committed complete-manifest fixture is the positive case: one real
manifest over the ``epoch1-full`` corpus, carrying all 38 declared
collections, both drop-proof forms and a valid seal. Every negative case
here starts from that fixture and breaks exactly one rule, which is what
makes the failure attributable -- a manifest that fails for two reasons
proves nothing about either.

The fixture is a loader fixture, not a golden of the current importer: it
is deliberately not compared against a freshly built plan, so an upstream
rule-payload edit does not red this file. The plan-mode integration test
owns the live-build assertions.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.dispositions import Disposition, DropProofForm, StorageTier
from eawf.kernel.migration.epoch2.lifecycle import DeferralReason
from eawf.kernel.migration.epoch2.manifest import (
    MANIFEST_SCHEMA_VERSION,
    VALIDATION_PASS_COUNT,
    BackupRecord,
    GitEvidence,
    GitFact,
    GitFactKind,
    MigrationManifest,
    OperatorAssignment,
    RollbackBoundary,
    RowMapping,
    SealState,
    StoreMapping,
    TargetCensus,
    TierPlacement,
    UnresolvedReason,
    UnresolvedRow,
    idempotence_payload,
    seal_digest_of,
)
from eawf.kernel.migration.epoch2.rows import SourceShape
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.kernel.store.tiers import StorageTier as TierTableStorageTier

COMPLETE_MANIFEST = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "migration"
    / "epoch2"
    / "complete-manifest.json"
)

#: A collection the ``epoch1-full`` corpus serialized as JSON ``null``, so
#: its row mapping carries the ``null_not_empty`` proof form.
NULL_SLOT = "tracks"

#: A collection that corpus holds as a present-but-empty container.
ZERO_ROW_SLOT = "plugins"

#: The collection whose Track ownership the source cannot supply.
GOALS_COLLECTION = "goals"

#: A restore point and a tier placement the staged-write cases reuse. Both
#: are built through their own constructors so the digests they carry are
#: the ones the models derive, which is what the negative cases then break.
_BACKUP = BackupRecord.of(
    taken_at=datetime(2026, 1, 1, tzinfo=UTC),
    surfaces=("state.json@sha256:" + "a" * 64,),
)
_PLACEMENT = TierPlacement.of(
    records_by_tier={
        TierTableStorageTier.DOCUMENT: 3,
        TierTableStorageTier.LEDGER: 511,
    },
    document_record_count=3,
    document_byte_length=4940,
    ledger_byte_length=436_017,
    indexed_collections=(Epoch2Collection.TASK,),
)


@pytest.fixture(scope="module")
def manifest_json() -> bytes:
    """The committed complete-manifest fixture, read once."""
    return COMPLETE_MANIFEST.read_bytes()


@pytest.fixture(scope="module")
def complete(manifest_json: bytes) -> MigrationManifest:
    """The fixture loaded through the strict model."""
    return MigrationManifest.model_validate_json(manifest_json)


@pytest.fixture(scope="module")
def payload(manifest_json: bytes) -> dict[str, Any]:
    """The fixture as a raw dict, for the negative cases to mutate."""
    loaded: dict[str, Any] = json.loads(manifest_json)
    return loaded


def _broken(
    payload: dict[str, Any], mutate: Callable[[dict[str, Any]], None]
) -> pytest.ExceptionInfo[ValidationError]:
    """Apply one mutation to a copy of ``payload`` and capture the refusal.

    Args:
        payload: The valid manifest payload.
        mutate: The single rule-breaking edit, applied in place.

    Returns:
        The captured validation error.
    """
    candidate = copy.deepcopy(payload)
    mutate(candidate)
    with pytest.raises(ValidationError) as excinfo:
        MigrationManifest.model_validate(candidate)
    return excinfo


def _row(payload: dict[str, Any], collection: str) -> dict[str, Any]:
    """Return the row mapping of ``collection`` inside a raw payload."""
    rows: list[dict[str, Any]] = payload["row_mappings"]
    for row in rows:
        if row["source_collection"] == collection:
            return row
    raise KeyError(collection)


def test_complete_manifest_fixture_loads(complete: MigrationManifest) -> None:
    """The fixture is a valid manifest of exactly the contracted shape."""
    assert complete.schema_version == MANIFEST_SCHEMA_VERSION
    assert len(MigrationManifest.model_fields) == 21
    assert len(complete.row_mappings) == 38
    assert complete.seal_state is SealState.SEALED
    assert complete.rollback_boundary is RollbackBoundary.PLAN_ONLY
    assert complete.backup is None
    assert complete.tier_placement is None
    assert len(complete.validation_results) == VALIDATION_PASS_COUNT


def test_complete_manifest_carries_every_zero_row_collection(
    complete: MigrationManifest,
) -> None:
    """Both drop-proof forms appear, and no proof row claims a target."""
    proofs = {
        row.source_collection: row.proof_form
        for row in complete.row_mappings
        if row.proof_form is not None
    }
    assert proofs[NULL_SLOT] is DropProofForm.NULL_NOT_EMPTY
    assert proofs[ZERO_ROW_SLOT] is DropProofForm.ZERO_ROWS
    assert all(complete.row_mapping(name).accounted_row_count == 0 for name in proofs)


def test_complete_manifest_separates_assignments_from_unresolved_rows(
    complete: MigrationManifest,
) -> None:
    """The Track an outcome hangs off is an assignment, not an unresolved row."""
    assignment = complete.assignments_for(GOALS_COLLECTION)
    assert [row.address for row in assignment] == ["goals/G01"]
    assert assignment[0].reason is DeferralReason.SOURCE_HAS_NO_FIELD
    assert GOALS_COLLECTION not in {row.source_collection for row in complete.unresolved_rows}


def test_manifest_missing_zero_row_collection_fails(payload: dict[str, Any]) -> None:
    """Dropping a zero-row collection's mapping loses the proof of its fate."""

    def drop_null_slot(candidate: dict[str, Any]) -> None:
        candidate["row_mappings"] = [
            row for row in candidate["row_mappings"] if row["source_collection"] != NULL_SLOT
        ]

    message = str(_broken(payload, drop_null_slot).value)
    assert "omits 1 declared collections" in message
    assert NULL_SLOT in message


def test_manifest_unknown_disposition_string_fails(payload: dict[str, Any]) -> None:
    """A disposition outside the closed vocabulary never parses."""

    def unknown_disposition(candidate: dict[str, Any]) -> None:
        _row(candidate, "phases")["disposition"] = "best_effort_conversion"

    assert "best_effort_conversion" in str(_broken(payload, unknown_disposition).value)


def test_manifest_disposition_contradicting_the_table_fails(
    payload: dict[str, Any],
) -> None:
    """A valid disposition the table does not declare is still a refusal."""

    def swap_disposition(candidate: dict[str, Any]) -> None:
        _row(candidate, "phases")["disposition"] = Disposition.EXPLICIT_DROP.value

    message = str(_broken(payload, swap_disposition).value)
    assert "contradicts its declared tables" in message
    assert "disposition" in message


def test_manifest_unresolved_alias_fails(payload: dict[str, Any]) -> None:
    """Two source rows resolving to one record cannot be sealed over."""

    def collide_aliases(candidate: dict[str, Any]) -> None:
        for record in candidate["validation_results"]:
            record["minted_records"] = record["aliased_rows"] - 1
            record["alias_collisions"] = [
                {
                    "target_urn": "eawf://WSP-DEFAULT/PRJ-DEMO/REP-DEMO/task/DEMO-0001",
                    "source_addresses": ["waves/P01-I01-W01", "backlog/B001"],
                }
            ]

    message = str(_broken(payload, collide_aliases).value)
    assert "an epoch-1 identifier is unresolved" in message
    assert "DEMO-0001" in message


def test_manifest_inferred_acceptance_annotation_fails(payload: dict[str, Any]) -> None:
    """An acceptance the source never proved must not reach a manifest."""

    def fabricate_acceptance(candidate: dict[str, Any]) -> None:
        for record in candidate["validation_results"]:
            record["fabrication_findings"] = [
                {
                    "address": "phases/P01",
                    "reason": "acceptance_without_source_proof",
                    "field": "accepted_binding",
                }
            ]

    message = str(_broken(payload, fabricate_acceptance).value)
    assert "rows the source does not entail" in message
    assert "acceptance_without_source_proof" in message


def test_manifest_target_count_above_the_census_fails(payload: dict[str, Any]) -> None:
    """A mapping that writes more than the census totals is a mismatch."""

    def inflate_target(candidate: dict[str, Any]) -> None:
        _row(candidate, "phases")["target_row_count"] += 1

    message = str(_broken(payload, inflate_target).value)
    assert "but the target census totals" in message


def test_manifest_target_count_below_the_source_fails(payload: dict[str, Any]) -> None:
    """A source row the manifest neither converts nor names is a mismatch."""

    def lose_a_row(candidate: dict[str, Any]) -> None:
        row = _row(candidate, "phases")
        row["target_row_count"] -= 1

    message = str(_broken(payload, lose_a_row).value)
    assert "record(s) to account for" in message


def test_manifest_unresolved_tally_must_match_the_listed_rows(
    payload: dict[str, Any],
) -> None:
    """A counted unresolved row that is not listed cannot be acted on."""

    def hide_a_row(candidate: dict[str, Any]) -> None:
        candidate["unresolved_rows"] = [
            row for row in candidate["unresolved_rows"] if row["source_collection"] != "incidents"
        ]

    message = str(_broken(payload, hide_a_row).value)
    assert "per-collection tallies do not match" in message


def test_manifest_diverging_validation_passes_fail(payload: dict[str, Any]) -> None:
    """Two passes that are not byte-identical prove the import is not a function."""

    def diverge(candidate: dict[str, Any]) -> None:
        candidate["validation_results"][1]["payload_byte_length"] += 1

    assert "not byte-identical" in str(_broken(payload, diverge).value)


def test_manifest_single_validation_pass_fails(payload: dict[str, Any]) -> None:
    """One pass is a result; two are a proof."""

    def drop_second(candidate: dict[str, Any]) -> None:
        candidate["validation_results"] = candidate["validation_results"][:1]

    assert "2 validation passes" in str(_broken(payload, drop_second).value)


def test_manifest_stale_content_digest_fails(payload: dict[str, Any]) -> None:
    """A digest that no longer covers the content is refused at load."""

    def stale_digest(candidate: dict[str, Any]) -> None:
        candidate["manifest_digest"] = "0" * 64

    assert "does not cover its content" in str(_broken(payload, stale_digest).value)


def test_manifest_source_digest_must_name_the_pinned_snapshot(
    payload: dict[str, Any],
) -> None:
    """The named revision and the pinned snapshot are one fact, not two."""

    def mismatch(candidate: dict[str, Any]) -> None:
        candidate["source_digest"] = "f" * 64

    assert "but pins snapshot" in str(_broken(payload, mismatch).value)


def test_manifest_draft_with_a_seal_field_fails(payload: dict[str, Any]) -> None:
    """A draft is unsealed; a half-sealed draft is neither."""

    def half_seal(candidate: dict[str, Any]) -> None:
        candidate["seal_state"] = SealState.DRAFT.value

    assert "one of its seal fields is set" in str(_broken(payload, half_seal).value)


def test_manifest_incomplete_seal_fails(payload: dict[str, Any]) -> None:
    """A seal records when, who and what; two of three is not a seal."""

    def drop_principal(candidate: dict[str, Any]) -> None:
        candidate["sealed_by"] = None

    assert "one of the three is missing" in str(_broken(payload, drop_principal).value)


def test_manifest_seal_digest_must_bind_the_content_digest(
    payload: dict[str, Any],
) -> None:
    """Re-sealing under a different principal invalidates the old seal."""

    def retag_principal(candidate: dict[str, Any]) -> None:
        candidate["sealed_by"] = "someone-else"

    assert "does not bind content digest" in str(_broken(payload, retag_principal).value)


def test_manifest_plan_only_boundary_admits_no_backup(payload: dict[str, Any]) -> None:
    """Nothing written means nothing to restore."""

    def add_backup(candidate: dict[str, Any]) -> None:
        candidate["backup"] = _BACKUP.model_dump(mode="json")

    assert "carries a backup" in str(_broken(payload, add_backup).value)


def test_manifest_past_the_first_write_requires_a_backup(
    payload: dict[str, Any],
) -> None:
    """Past the first durable write, a manifest with no restore point is a trap."""

    def advance_boundary(candidate: dict[str, Any]) -> None:
        candidate["rollback_boundary"] = RollbackBoundary.MARKER_WRITTEN.value

    assert "no backup to restore from" in str(_broken(payload, advance_boundary).value)


def test_manifest_plan_only_boundary_admits_no_tier_placement(
    payload: dict[str, Any],
) -> None:
    """A plan that wrote nothing cannot have measured a tier layout."""

    def add_placement(candidate: dict[str, Any]) -> None:
        candidate["tier_placement"] = _PLACEMENT.model_dump(mode="json")

    assert "has written nothing into a tier" in str(_broken(payload, add_placement).value)


def test_manifest_past_the_staged_write_requires_a_tier_placement(
    payload: dict[str, Any],
) -> None:
    """A staged cutover that reports no placement claims a layout nobody measured."""

    def advance_boundary(candidate: dict[str, Any]) -> None:
        candidate["rollback_boundary"] = RollbackBoundary.STAGED.value
        candidate["backup"] = _BACKUP.model_dump(mode="json")

    assert "reports no tier placement" in str(_broken(payload, advance_boundary).value)


def test_staging_a_manifest_records_the_placement_and_keeps_the_seal(
    complete: MigrationManifest,
) -> None:
    """Recording a staged write moves the boundary and nothing the seal covers."""
    staged = complete.staged(placement=_PLACEMENT, backup=_BACKUP)

    assert staged.rollback_boundary is RollbackBoundary.STAGED
    assert staged.tier_placement == _PLACEMENT
    assert staged.backup == _BACKUP
    assert staged.manifest_digest == complete.manifest_digest
    assert staged.seal_digest == complete.seal_digest
    assert staged.idempotence_digest == complete.idempotence_digest


def test_tier_placement_digest_covers_its_counts() -> None:
    """A hand-edited byte count no longer matches the digest over it."""
    payload = _PLACEMENT.model_dump(mode="json")
    payload["document_byte_length"] = _PLACEMENT.document_byte_length + 1
    with pytest.raises(ValidationError) as excinfo:
        TierPlacement.model_validate(payload)
    assert "does not cover its counts" in str(excinfo.value)


def test_tier_placement_rejects_an_undeclared_tier() -> None:
    """A tier the table never declared has no file for records to land in."""
    candidate = _PLACEMENT.model_dump(mode="json")
    candidate["records_by_tier"] = {"warm_cache": 3}
    with pytest.raises(ValidationError) as excinfo:
        TierPlacement.model_validate(candidate)
    assert "which no storage tier declares" in str(excinfo.value)


def test_tier_placement_refuses_a_residual_the_document_tier_disagrees_with() -> None:
    """The residual count and the document tier's count are one number."""
    with pytest.raises(ValidationError) as excinfo:
        TierPlacement.of(
            records_by_tier={TierTableStorageTier.DOCUMENT: 2},
            document_record_count=5,
            document_byte_length=64,
            ledger_byte_length=0,
            indexed_collections=(),
        )
    assert "residual document records" in str(excinfo.value)


def test_tier_placement_refuses_a_non_empty_document_of_zero_bytes() -> None:
    """A document holding rows cannot weigh nothing."""
    with pytest.raises(ValidationError) as excinfo:
        TierPlacement.of(
            records_by_tier={TierTableStorageTier.DOCUMENT: 1},
            document_record_count=1,
            document_byte_length=0,
            ledger_byte_length=0,
            indexed_collections=(),
        )
    assert "zero bytes" in str(excinfo.value)


def test_empty_tier_placement_is_a_valid_record_of_an_empty_write() -> None:
    """A write that placed nothing is a placement, not a missing one."""
    placement = TierPlacement.of(
        records_by_tier={},
        document_record_count=0,
        document_byte_length=0,
        ledger_byte_length=0,
        indexed_collections=(),
    )
    assert placement.total_records == 0
    assert placement.records_in(TierTableStorageTier.LEDGER) == 0


def test_backup_record_digest_covers_its_surfaces() -> None:
    """A surface added by hand no longer matches the digest over the set."""
    payload = _BACKUP.model_dump(mode="json")
    payload["surfaces"] = [*_BACKUP.surfaces, "ledger/task.jsonl@sha256:" + "b" * 64]
    with pytest.raises(ValidationError) as excinfo:
        BackupRecord.model_validate(payload)
    assert "does not cover its" in str(excinfo.value)


def test_sealing_a_draft_preserves_the_content_digest(
    complete: MigrationManifest,
) -> None:
    """A second seal over one content changes only the seal."""
    resealed = complete.sealed(
        sealed_at=datetime(2027, 6, 1, tzinfo=UTC), sealed_by="second-operator"
    )
    assert resealed.manifest_digest == complete.manifest_digest
    assert resealed.seal_digest != complete.seal_digest
    assert resealed.sealed_by == "second-operator"


def test_seal_digest_covers_every_seal_field() -> None:
    """Each of the three sealed facts moves the digest."""
    base = seal_digest_of(
        manifest_digest="b" * 64, sealed_at=datetime(2026, 1, 1, tzinfo=UTC), sealed_by="a"
    )
    assert base != seal_digest_of(
        manifest_digest="c" * 64, sealed_at=datetime(2026, 1, 1, tzinfo=UTC), sealed_by="a"
    )
    assert base != seal_digest_of(
        manifest_digest="b" * 64, sealed_at=datetime(2026, 1, 2, tzinfo=UTC), sealed_by="a"
    )
    assert base != seal_digest_of(
        manifest_digest="b" * 64, sealed_at=datetime(2026, 1, 1, tzinfo=UTC), sealed_by="b"
    )


def test_idempotence_payload_ignores_how_the_plan_was_reached(
    complete: MigrationManifest,
) -> None:
    """The placement digest covers what is written, not how it was decided."""
    payload = idempotence_payload(
        target_census=complete.target_census,
        row_mappings=complete.row_mappings,
        store_mappings=complete.store_mappings,
    )
    assert set(payload) == {"target_census", "row_mappings", "store_mappings"}


def test_row_mapping_lookup_rejects_an_undeclared_collection(
    complete: MigrationManifest,
) -> None:
    """A collection with no mapping is a lookup failure, never a default."""
    with pytest.raises(KeyError):
        complete.row_mapping("no_such_collection")


def test_row_mapping_undeclared_collection_fails() -> None:
    """A row mapping for a collection nobody declared cannot be built."""
    with pytest.raises(ValidationError, match="has no disposition row"):
        RowMapping(
            source_collection="invented",
            disposition=Disposition.NATIVE_CONVERSION,
            target_collection="task",
            source_tier=StorageTier.DOCUMENT,
            shape=SourceShape.KEYED_ROWS,
            source_row_count=1,
            target_row_count=1,
            unresolved_row_count=0,
            proof_form=None,
            operator_assignment_count=0,
        )


def test_row_mapping_metadata_key_holds_no_rows() -> None:
    """Document metadata with a row count is a contradiction."""
    with pytest.raises(ValidationError, match="is document metadata"):
        RowMapping(
            source_collection="schema_version",
            disposition=None,
            target_collection="-",
            source_tier=StorageTier.METADATA,
            shape=SourceShape.SCALAR,
            source_row_count=1,
            target_row_count=0,
            unresolved_row_count=0,
            proof_form=None,
            operator_assignment_count=0,
        )


def test_row_mapping_drop_proof_over_rows_fails() -> None:
    """A drop proof asserts nothing to import; rows contradict it."""
    with pytest.raises(ValidationError, match="proof form, which asserts nothing"):
        RowMapping(
            source_collection="plugins",
            disposition=Disposition.IMMUTABLE_LEGACY_RECORD,
            target_collection="legacy",
            source_tier=StorageTier.LEDGER,
            shape=SourceShape.KEYED_ROWS,
            source_row_count=4,
            target_row_count=0,
            unresolved_row_count=0,
            proof_form=DropProofForm.ZERO_ROWS,
            operator_assignment_count=0,
        )


def test_row_mapping_explicit_drop_writes_nothing() -> None:
    """An explicit drop that writes a record is not a drop."""
    with pytest.raises(ValidationError, match="is an explicit drop"):
        RowMapping(
            source_collection="close_attempts",
            disposition=Disposition.EXPLICIT_DROP,
            target_collection="-",
            source_tier=StorageTier.NONE,
            shape=SourceShape.KEYED_ROWS,
            source_row_count=1,
            target_row_count=1,
            unresolved_row_count=0,
            proof_form=None,
            operator_assignment_count=0,
        )


def test_row_mapping_singleton_mapping_owes_one_record() -> None:
    """A non-row mapping is one entity however many fields it holds."""
    mapping = RowMapping(
        source_collection="project",
        disposition=Disposition.NATIVE_CONVERSION,
        target_collection="project",
        source_tier=StorageTier.DOCUMENT,
        shape=SourceShape.MAPPING,
        source_row_count=3,
        target_row_count=0,
        unresolved_row_count=1,
        proof_form=None,
        operator_assignment_count=0,
    )
    assert mapping.accounted_row_count == 1


def test_store_mapping_tier_must_match_the_tier_table() -> None:
    """A manifest cannot route bytes to a tier the table forbids."""
    with pytest.raises(ValidationError, match="is declared at tier ledger"):
        StoreMapping(
            collection=Epoch2Collection.TASK,
            tier=TierTableStorageTier.DOCUMENT,
            row_count=1,
        )


def test_store_mapping_rejects_an_empty_route() -> None:
    """A collection receiving no record has no store mapping at all."""
    with pytest.raises(ValidationError):
        StoreMapping(
            collection=Epoch2Collection.TASK,
            tier=TierTableStorageTier.LEDGER,
            row_count=0,
        )


def test_target_census_groupings_must_reconcile() -> None:
    """A census whose groupings disagree with its total describes nothing."""
    with pytest.raises(ValidationError, match="collection grouping sums to"):
        TargetCensus(
            total_rows=5,
            planned_rows=0,
            by_entity_kind={"task": 5},
            by_collection={"task": 4},
            census_digest="0" * 64,
        )


def test_target_census_of_empty_import_is_total() -> None:
    """An import that writes nothing still censuses to a consistent zero."""
    census = TargetCensus.of(by_entity_kind={}, by_collection={}, planned_rows=0)
    assert census.total_rows == 0
    assert census.by_collection == {}


def test_target_census_planned_rows_are_excluded_from_the_kind_grouping() -> None:
    """A record with no addressable kind is counted once, by collection only."""
    census = TargetCensus.of(
        by_entity_kind={"task": 2}, by_collection={"task": 2, "track_outcome": 1}, planned_rows=1
    )
    assert census.total_rows == 3
    assert census.planned_rows == 1


def test_target_census_planned_rows_above_the_total_fails() -> None:
    """More planned records than records is not a census."""
    with pytest.raises(ValidationError, match="entity-kind"):
        TargetCensus.of(by_entity_kind={"task": 2}, by_collection={"task": 2}, planned_rows=1)


def test_evidence_digest_covers_the_fact_list() -> None:
    """Editing a recorded reference invalidates the evidence block."""
    fact = GitFact(
        address="waves/P01-I01-W01",
        field="commit",
        kind=GitFactKind.COMMIT,
        value="0f1e2d3",
    )
    evidence = GitEvidence.of((fact,))
    assert evidence.facts_of_kind(GitFactKind.COMMIT) == (fact,)
    assert evidence.facts_of_kind(GitFactKind.CANDIDATE_TAG) == ()
    with pytest.raises(ValidationError, match="does not cover its"):
        GitEvidence(facts=(), evidence_digest=evidence.evidence_digest)


def test_evidence_of_no_facts_is_still_digested() -> None:
    """A corpus that recorded no reference says so with a digest, not a gap."""
    evidence = GitEvidence.of(())
    assert evidence.facts == ()
    assert len(evidence.evidence_digest) == 64


def test_unresolved_row_requires_a_reason_from_the_vocabulary() -> None:
    """An unresolved row names why, from a closed list."""
    row = UnresolvedRow(
        address="decisions/D01",
        source_collection="decisions",
        reason=UnresolvedReason.NO_CONVERTER,
        detail="no importer rule implements the declared conversion",
    )
    assert row.reason is UnresolvedReason.NO_CONVERTER
    with pytest.raises(ValidationError):
        UnresolvedRow.model_validate(
            {
                "address": "decisions/D01",
                "source_collection": "decisions",
                "reason": "unknown_reason",
                "detail": "x",
            }
        )


def test_operator_assignment_rejects_an_empty_candidate() -> None:
    """An empty candidate string is not a choice an operator can make."""
    with pytest.raises(ValidationError):
        OperatorAssignment(
            address="phases/P01",
            source_collection="phases",
            target_collection="milestone",
            target_field="primary_track_ref",
            reason=DeferralReason.AMBIGUOUS_SOURCE_CANDIDATES,
            candidates=("TRK-A", ""),
        )


def test_backup_record_rejects_a_malformed_digest() -> None:
    """A restore point is only as good as the digest it restores against."""
    with pytest.raises(ValidationError):
        BackupRecord(
            taken_at=datetime(2026, 1, 1, tzinfo=UTC),
            surfaces=("document.json",),
            backup_digest="not-a-digest",
        )


def test_manifest_rejects_an_extra_field(payload: dict[str, Any]) -> None:
    """An unknown field is a schema the loader has never seen."""

    def add_field(candidate: dict[str, Any]) -> None:
        candidate["replication_factor"] = 3

    assert "replication_factor" in str(_broken(payload, add_field).value)


def test_manifest_rejects_a_foreign_schema_version(payload: dict[str, Any]) -> None:
    """An epoch-1 version string is not an epoch-2 manifest."""

    def downgrade(candidate: dict[str, Any]) -> None:
        candidate["schema_version"] = "1.19"

    assert "schema_version" in str(_broken(payload, downgrade).value)
