"""The sealable record of one epoch-1 to epoch-2 cutover.

A manifest is the one object the cutover, the rollback boundary and the
release signal all read, so it is strict to the point of being
self-checking: it recomputes its own digests at load, pins every row
mapping against the disposition table, and refuses a validation pass
that found an unresolvable alias or a fabricated row. A manifest that
loads is therefore a manifest whose claims have been re-derived, not one
whose claims were taken on trust.

Totality is the other half. The manifest carries one row mapping per
declared source collection, including the collections that hold nothing:
a collection missing from the mapping table is a collection whose fate
nobody recorded, and that is the failure mode the whole contract exists
to make impossible.

The manifest distinguishes two ways a source row can fall short of a
finished import. A **required operator assignment** is a field the source
never recorded -- which Track owns a Milestone, which Track an outcome
metric hangs off -- and the row imports without it, annotated. An
**unresolved row** is a row the cutover cannot place at all, and it
refuses the apply. Collapsing the two would either block a cutover on
questions an operator can answer afterwards, or quietly write rows whose
target nobody decided.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    Disposition,
)
from eawf.kernel.migration.epoch2.manifest_rows import (
    GIT_EVIDENCE_FIELDS,
    VALIDATION_PASS_COUNT,
    BackupRecord,
    GitEvidence,
    GitFact,
    GitFactKind,
    ManifestSource,
    OperatorAssignment,
    RollbackBoundary,
    RowMapping,
    SealState,
    StoreMapping,
    TargetCensus,
    TierPlacement,
    UnresolvedReason,
    UnresolvedRow,
    ValidationPass,
)
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    StrictMigrationModel,
    rule_digest,
)
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


#: The schema version every epoch-2 manifest carries. Epoch 1's state
#: document versioned as ``1.<n>``; the cutover starts a new series so a
#: reader can tell the two document families apart by one field.
MANIFEST_SCHEMA_VERSION: Final[Literal["2"]] = "2"

#: The epoch-2 field a Milestone's Track ownership lands in.
MILESTONE_TRACK_FIELD = "primary_track_ref"

#: The epoch-2 field a Track outcome metric's owning Track lands in.
OUTCOME_TRACK_FIELD = "track_ref"

#: The source collections whose rows convert into records the epoch-2
#: identity grammar does not address. A Track outcome hangs off a Track
#: rather than carrying a key of its own, so its rows are planned here
#: rather than minted through the identity traversal.
UNADDRESSED_TARGETS: Mapping[str, Epoch2Collection] = {
    "goals": Epoch2Collection.TRACK_OUTCOME,
    "outcomes": Epoch2Collection.TRACK_OUTCOME,
}

#: The dispositions under which a source row becomes a target record.
CONVERTING_DISPOSITIONS: frozenset[Disposition] = frozenset(
    {
        Disposition.NATIVE_CONVERSION,
        Disposition.IMMUTABLE_LEGACY_RECORD,
        Disposition.SPLIT_CONVERSION,
    }
)


class MigrationManifest(StrictMigrationModel):
    """The complete, self-checking record of one planned cutover.

    Attributes:
        schema_version: Always ``"2"``.
        source: The corpus revision and addressing slots.
        source_census: The epoch-1 inventory the plan was taken over.
        target_census: How many epoch-2 records the cutover writes.
        mapping_rules: Every importer rule version the plan ran under.
        row_mappings: One row per declared source collection.
        store_mappings: One row per epoch-2 collection receiving records.
        git_evidence: Every git reference the source recorded.
        track_assignments: The Track ownership an operator must supply.
        unresolved_rows: Every row the cutover cannot place.
        source_digest: The pinned revision of the corpus.
        manifest_digest: A digest over every content field, excluding the
            three digests and the seal, so two plans over one revision
            agree on it even though they were sealed at different times.
        idempotence_digest: A digest over what a second apply must
            reproduce exactly.
        backup: The restore point, once an apply has taken one.
        tier_placement: What a staged write left in each tier, once one
            has run. ``None`` while the cutover is still a plan, because
            nothing has been written to measure.
        rollback_boundary: How far the cutover has gone.
        validation_results: The two validation passes, byte-identical.
        seal_state: Whether the manifest is closed.
        sealed_at: When it was sealed.
        sealed_by: The principal that sealed it.
        seal_digest: A digest binding the content digest to the seal.
    """

    schema_version: Literal["2"]
    source: ManifestSource
    source_census: SourceCensus
    target_census: TargetCensus
    mapping_rules: tuple[MappingRuleVersion, ...]
    row_mappings: tuple[RowMapping, ...]
    store_mappings: tuple[StoreMapping, ...]
    git_evidence: GitEvidence
    track_assignments: tuple[OperatorAssignment, ...]
    unresolved_rows: tuple[UnresolvedRow, ...]
    source_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    idempotence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    backup: BackupRecord | None
    tier_placement: TierPlacement | None
    rollback_boundary: RollbackBoundary
    validation_results: tuple[ValidationPass, ...]
    seal_state: SealState
    sealed_at: datetime | None
    sealed_by: Annotated[str, Field(min_length=1, max_length=64)] | None
    seal_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None

    @model_validator(mode="after")
    def _row_mappings_are_total(self) -> Self:
        """Refuse a mapping table that is not a bijection on the source.

        Returns:
            The validated manifest.

        Raises:
            ValueError: When a declared collection has no row mapping,
                when one has two, or when a mapping contradicts the
                census row for the same collection. A collection with no
                row is a collection whose fate nobody recorded.
        """
        _require_mappings_total(self)
        _require_mappings_match_census(self)
        return self

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        """Refuse a manifest whose censuses and mappings disagree.

        Returns:
            The validated manifest.

        Raises:
            ValueError: When the target census, the row mappings, the
                store mappings, the unresolved rows or the Track
                assignments do not agree on their counts.
        """
        _require_target_census_matches_mappings(self)
        _require_store_mappings_match_census(self)
        _require_per_collection_tallies(self)
        return self

    @model_validator(mode="after")
    def _validation_passes_agree(self) -> Self:
        """Refuse anything but two clean, byte-identical validation passes.

        Returns:
            The validated manifest.

        Raises:
            ValueError: When the pass count or indices are wrong, when
                the two digests differ, or when either pass found an
                unresolvable alias, a dangling reference or a row the
                source does not entail.
        """
        _require_two_agreeing_passes(self)
        for record in self.validation_results:
            _require_pass_clean(record)
        return self

    @model_validator(mode="after")
    def _digests_are_mutually_consistent(self) -> Self:
        """Recompute all four digests and refuse any that does not hold.

        Returns:
            The validated manifest.

        Raises:
            ValueError: When the source digest does not name the pinned
                snapshot, when the content or idempotence digest does not
                cover its fields, or when the seal fields are not
                mutually consistent.
        """
        _require_digests_consistent(self)
        _require_seal_consistent(self)
        return self

    def row_mapping(self, source_collection: str) -> RowMapping:
        """Return the row mapping of one source collection.

        Args:
            source_collection: The epoch-1 top-level key.

        Returns:
            Its row mapping.

        Raises:
            KeyError: When the manifest carries no mapping for it, which
                totality makes unreachable for a declared collection.
        """
        for mapping in self.row_mappings:
            if mapping.source_collection == source_collection:
                return mapping
        raise KeyError(source_collection)

    def assignments_for(self, source_collection: str) -> tuple[OperatorAssignment, ...]:
        """Return the required operator assignments one collection raised.

        Args:
            source_collection: The epoch-1 top-level key.

        Returns:
            Its assignments, in manifest order.
        """
        return tuple(
            row for row in self.track_assignments if row.source_collection == source_collection
        )

    def sealed(self, *, sealed_at: datetime, sealed_by: str) -> MigrationManifest:
        """Return this manifest closed under a seal.

        The seal covers the content digest rather than the whole
        document, so sealing the same content twice yields the same
        ``manifest_digest`` and a different ``seal_digest`` -- which is
        what lets two plan runs be compared while each still records who
        closed it and when.

        Args:
            sealed_at: When the seal was taken.
            sealed_by: The principal taking it.

        Returns:
            The sealed manifest.

        Raises:
            ValidationError: When a seal field violates the contract.
        """
        payload = self.model_dump(mode="json")
        payload["seal_state"] = SealState.SEALED.value
        payload["sealed_at"] = sealed_at.isoformat()
        payload["sealed_by"] = sealed_by
        payload["seal_digest"] = seal_digest_of(
            manifest_digest=self.manifest_digest,
            sealed_at=sealed_at,
            sealed_by=sealed_by,
        )
        return MigrationManifest.model_validate(payload)

    def staged(self, *, placement: TierPlacement, backup: BackupRecord) -> MigrationManifest:
        """Return this manifest with one staged write recorded against it.

        The placement and the backup are excluded from the content digest,
        so recording them moves the rollback boundary without invalidating
        the seal an operator approved the plan under.

        Args:
            placement: What the staged write left in each tier.
            backup: The restore point taken before the first durable
                write.

        Returns:
            The manifest at the ``staged`` rollback boundary.

        Raises:
            ValidationError: When the placement contradicts its own
                counts, or when the boundary and the recorded fields
                disagree.
        """
        payload = self.model_dump(mode="json")
        payload["tier_placement"] = placement.model_dump(mode="json")
        payload["backup"] = backup.model_dump(mode="json")
        payload["rollback_boundary"] = RollbackBoundary.STAGED.value
        return MigrationManifest.model_validate(payload)

    def advanced(self, *, boundary: RollbackBoundary) -> MigrationManifest:
        """Return this manifest with the rollback boundary moved forward.

        The boundary is a record of what is on disk, so it only ever
        moves in the direction the durable writes go. Refusing a
        backwards move here is what stops a later stage from recording a
        cutover as less far along than it is -- the one error that turns
        a recoverable crash into a second write over a selected
        generation.

        Args:
            boundary: The boundary the cutover has now reached.

        Returns:
            The manifest at ``boundary``.

        Raises:
            ValueError: ``boundary`` is at or behind the one already
                recorded.
            ValidationError: The new boundary contradicts the recorded
                placement or backup.
        """
        order = tuple(RollbackBoundary)
        if order.index(boundary) <= order.index(self.rollback_boundary):
            raise ValueError(
                f"the cutover is already at {self.rollback_boundary.value}, so it cannot "
                f"be recorded as reaching {boundary.value}"
            )
        payload = self.model_dump(mode="json")
        payload["rollback_boundary"] = boundary.value
        return MigrationManifest.model_validate(payload)


def seal_digest_of(*, manifest_digest: str, sealed_at: datetime, sealed_by: str) -> str:
    """Return the digest binding one content digest to one seal.

    Args:
        manifest_digest: The content digest being sealed.
        sealed_at: When the seal was taken.
        sealed_by: The principal taking it.

    Returns:
        A 64-character lowercase hex digest.
    """
    return rule_digest(
        {
            "manifest_digest": manifest_digest,
            "sealed_at": sealed_at.isoformat(),
            "sealed_by": sealed_by,
        }
    )


def manifest_content_payload(
    *,
    source: ManifestSource,
    source_census: SourceCensus,
    target_census: TargetCensus,
    mapping_rules: tuple[MappingRuleVersion, ...],
    row_mappings: tuple[RowMapping, ...],
    store_mappings: tuple[StoreMapping, ...],
    git_evidence: GitEvidence,
    track_assignments: tuple[OperatorAssignment, ...],
    unresolved_rows: tuple[UnresolvedRow, ...],
    validation_results: tuple[ValidationPass, ...],
) -> dict[str, Any]:
    """Return the exact payload :attr:`MigrationManifest.manifest_digest` covers.

    The backup, the rollback boundary and the seal are excluded on
    purpose: all three move as a cutover proceeds, and a content digest
    that moved with them could not be compared across two plan runs.

    Args:
        source: The corpus revision and addressing slots.
        source_census: The epoch-1 inventory.
        target_census: The epoch-2 record counts.
        mapping_rules: The rule versions the plan ran under.
        row_mappings: One row per declared source collection.
        store_mappings: One row per epoch-2 collection receiving records.
        git_evidence: The git references the source recorded.
        track_assignments: The Track ownership an operator must supply.
        unresolved_rows: Every row the cutover cannot place.
        validation_results: The validation passes.

    Returns:
        The JSON-ready payload, in one fixed key order.
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": source.model_dump(mode="json"),
        "source_census": source_census.model_dump(mode="json"),
        "target_census": target_census.model_dump(mode="json"),
        "mapping_rules": [rule.model_dump(mode="json") for rule in mapping_rules],
        "row_mappings": [row.model_dump(mode="json") for row in row_mappings],
        "store_mappings": [row.model_dump(mode="json") for row in store_mappings],
        "git_evidence": git_evidence.model_dump(mode="json"),
        "track_assignments": [row.model_dump(mode="json") for row in track_assignments],
        "unresolved_rows": [row.model_dump(mode="json") for row in unresolved_rows],
        "validation_results": [row.model_dump(mode="json") for row in validation_results],
    }


def idempotence_payload(
    *,
    target_census: TargetCensus,
    row_mappings: tuple[RowMapping, ...],
    store_mappings: tuple[StoreMapping, ...],
) -> dict[str, Any]:
    """Return the payload a second apply has to reproduce exactly.

    Only the placement is in here -- what lands where, and how much of
    it. The censuses and the validation passes describe how the plan was
    reached; this describes what it writes, which is the thing a rerun
    must not change.

    Args:
        target_census: The epoch-2 record counts.
        row_mappings: One row per declared source collection.
        store_mappings: One row per epoch-2 collection receiving records.

    Returns:
        The JSON-ready payload, in one fixed key order.
    """
    return {
        "target_census": target_census.model_dump(mode="json"),
        "row_mappings": [row.model_dump(mode="json") for row in row_mappings],
        "store_mappings": [row.model_dump(mode="json") for row in store_mappings],
    }


def _require_mappings_total(manifest: MigrationManifest) -> None:
    """Refuse a mapping table that is not total over the declared collections.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When a declared collection is missing or repeated.
    """
    seen: list[str] = [row.source_collection for row in manifest.row_mappings]
    duplicates = sorted({name for name in seen if seen.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"{len(duplicates)} collections carry two row mappings: {', '.join(duplicates)}"
        )
    missing = sorted(frozenset(COLLECTION_DISPOSITION_INDEX) - frozenset(seen))
    if missing:
        raise ValueError(
            f"the manifest omits {len(missing)} declared collections, whose fate it "
            f"therefore cannot prove: {', '.join(missing)}"
        )


def _require_mappings_match_census(manifest: MigrationManifest) -> None:
    """Refuse a mapping whose source counts disagree with the census.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When a mapping's row count or proof form differs from
            the census row for the same collection.
    """
    problems: list[str] = []
    for mapping in manifest.row_mappings:
        row = manifest.source_census.collection(mapping.source_collection)
        if mapping.source_row_count != row.row_count:
            problems.append(
                f"{mapping.source_collection}: {mapping.source_row_count} rows mapped, "
                f"{row.row_count} censused"
            )
        if mapping.proof_form is not row.proof_form:
            problems.append(
                f"{mapping.source_collection}: proof form {mapping.proof_form} mapped, "
                f"{row.proof_form} censused"
            )
    if problems:
        raise ValueError(
            f"{len(problems)} row mappings disagree with the source census: {'; '.join(problems)}"
        )


def _require_target_census_matches_mappings(manifest: MigrationManifest) -> None:
    """Refuse a target census that does not sum the row mappings.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When the mapped target counts do not total the
            census.
    """
    mapped = sum(row.target_row_count for row in manifest.row_mappings)
    if mapped != manifest.target_census.total_rows:
        raise ValueError(
            f"the row mappings write {mapped} records but the target census totals "
            f"{manifest.target_census.total_rows}"
        )


def _require_store_mappings_match_census(manifest: MigrationManifest) -> None:
    """Refuse store mappings that do not match the target census by collection.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When a collection is routed twice, or when the routed
            counts differ from the census grouping.
    """
    routed: dict[str, int] = {}
    for row in manifest.store_mappings:
        key = row.collection.value
        if key in routed:
            raise ValueError(f"{key!r} carries two store mappings, so its tier is ambiguous")
        routed[key] = row.row_count
    if routed != manifest.target_census.by_collection:
        raise ValueError(
            f"the store mappings route {routed} but the target census groups "
            f"{manifest.target_census.by_collection}"
        )


def _require_per_collection_tallies(manifest: MigrationManifest) -> None:
    """Refuse a manifest whose itemised rows do not match its counts.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When the listed unresolved rows or Track assignments
            do not tally with the per-collection counts that summarise
            them.
    """
    unresolved: dict[str, int] = {}
    for open_row in manifest.unresolved_rows:
        unresolved[open_row.source_collection] = unresolved.get(open_row.source_collection, 0) + 1
    assignments: dict[str, int] = {}
    for deferred_row in manifest.track_assignments:
        assignments[deferred_row.source_collection] = (
            assignments.get(deferred_row.source_collection, 0) + 1
        )
    problems: list[str] = []
    for mapping in manifest.row_mappings:
        listed = unresolved.get(mapping.source_collection, 0)
        if listed != mapping.unresolved_row_count:
            problems.append(
                f"{mapping.source_collection}: {mapping.unresolved_row_count} unresolved "
                f"counted, {listed} listed"
            )
        deferred = assignments.get(mapping.source_collection, 0)
        if deferred != mapping.operator_assignment_count:
            problems.append(
                f"{mapping.source_collection}: {mapping.operator_assignment_count} "
                f"assignments counted, {deferred} listed"
            )
    if problems:
        raise ValueError(
            f"{len(problems)} per-collection tallies do not match the itemised rows: "
            f"{'; '.join(problems)}"
        )


def _require_two_agreeing_passes(manifest: MigrationManifest) -> None:
    """Refuse anything but two validation passes with one digest.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When the pass count or indices are wrong, or when the
            two passes disagree on their digest or payload length.
    """
    passes = manifest.validation_results
    if len(passes) != VALIDATION_PASS_COUNT:
        raise ValueError(
            f"a sealable manifest records {VALIDATION_PASS_COUNT} validation passes, "
            f"not {len(passes)}"
        )
    indices = tuple(row.pass_index for row in passes)
    if indices != tuple(range(1, VALIDATION_PASS_COUNT + 1)):
        raise ValueError(f"the validation passes are indexed {indices}, not 1 then 2")
    first, second = passes
    if first.report_digest != second.report_digest:
        raise ValueError(
            f"the two validation passes disagree: {first.report_digest} then "
            f"{second.report_digest}, so the import is not deterministic"
        )
    if first.payload_byte_length != second.payload_byte_length:
        raise ValueError(
            f"the two validation passes digest {first.payload_byte_length} then "
            f"{second.payload_byte_length} bytes, so they are not byte-identical"
        )


def _require_pass_clean(record: ValidationPass) -> None:
    """Refuse a validation pass a manifest must not be sealed over.

    Args:
        record: The pass to check.

    Raises:
        ValueError: When an epoch-1 identifier does not resolve once and
            injectively, when a canonical reference dangles, or when the
            import wrote a row the source does not entail.
    """
    if not record.aliases_resolve_injectively:
        collisions = ", ".join(row.target_urn for row in record.alias_collisions)
        raise ValueError(
            f"validation pass {record.pass_index} resolves {record.aliased_rows} aliases "
            f"onto {record.minted_records} records, so an epoch-1 identifier is "
            f"unresolved: {collisions or 'no collision listed'}"
        )
    if record.dangling_references:
        dangling = ", ".join(
            f"{row.source_address}.{row.field} -> {row.reference}"
            for row in record.dangling_references
        )
        raise ValueError(
            f"validation pass {record.pass_index} found {len(record.dangling_references)} "
            f"dangling references: {dangling}"
        )
    if record.fabrication_findings:
        findings = ", ".join(
            f"{row.address} {row.reason.value}" for row in record.fabrication_findings
        )
        raise ValueError(
            f"validation pass {record.pass_index} found "
            f"{len(record.fabrication_findings)} rows the source does not entail: {findings}"
        )


def _require_digests_consistent(manifest: MigrationManifest) -> None:
    """Recompute the three content digests and refuse a stale one.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When the source digest does not name the pinned
            snapshot, or when a digest does not cover its own payload.
    """
    pinned = manifest.source.snapshot.snapshot_digest
    if manifest.source_digest != pinned:
        raise ValueError(
            f"the manifest names source revision {manifest.source_digest} but pins "
            f"snapshot {pinned}"
        )
    if manifest.source_census.identity.snapshot_digest != pinned:
        raise ValueError(
            f"the source census covers revision "
            f"{manifest.source_census.identity.snapshot_digest}, not the pinned {pinned}"
        )
    expected_content = rule_digest(
        manifest_content_payload(
            source=manifest.source,
            source_census=manifest.source_census,
            target_census=manifest.target_census,
            mapping_rules=manifest.mapping_rules,
            row_mappings=manifest.row_mappings,
            store_mappings=manifest.store_mappings,
            git_evidence=manifest.git_evidence,
            track_assignments=manifest.track_assignments,
            unresolved_rows=manifest.unresolved_rows,
            validation_results=manifest.validation_results,
        )
    )
    if manifest.manifest_digest != expected_content:
        raise ValueError(
            f"manifest digest {manifest.manifest_digest} does not cover its content "
            f"(expected {expected_content})"
        )
    expected_idempotence = rule_digest(
        idempotence_payload(
            target_census=manifest.target_census,
            row_mappings=manifest.row_mappings,
            store_mappings=manifest.store_mappings,
        )
    )
    if manifest.idempotence_digest != expected_idempotence:
        raise ValueError(
            f"idempotence digest {manifest.idempotence_digest} does not cover the "
            f"planned placement (expected {expected_idempotence})"
        )


def _require_seal_consistent(manifest: MigrationManifest) -> None:
    """Refuse seal, backup and boundary fields that contradict each other.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When a draft carries a seal field, when a seal is
            incomplete or does not cover the content digest, or when the
            backup and the rollback boundary disagree about whether
            anything durable has been written.
    """
    seal_fields = (manifest.sealed_at, manifest.sealed_by, manifest.seal_digest)
    if manifest.seal_state is SealState.DRAFT:
        if any(field is not None for field in seal_fields):
            raise ValueError("a draft manifest carries no seal, but one of its seal fields is set")
    elif any(field is None for field in seal_fields):
        raise ValueError(
            "a sealed manifest records when it was sealed, who sealed it and the seal "
            "digest; one of the three is missing"
        )
    elif (
        manifest.sealed_at is not None
        and manifest.sealed_by is not None
        and manifest.seal_digest
        != seal_digest_of(
            manifest_digest=manifest.manifest_digest,
            sealed_at=manifest.sealed_at,
            sealed_by=manifest.sealed_by,
        )
    ):
        raise ValueError(
            f"seal digest {manifest.seal_digest} does not bind content digest "
            f"{manifest.manifest_digest} to this seal"
        )
    if manifest.rollback_boundary is RollbackBoundary.PLAN_ONLY and manifest.backup is not None:
        raise ValueError(
            "a plan-only manifest has written nothing to restore, but it carries a backup"
        )
    if manifest.rollback_boundary is not RollbackBoundary.PLAN_ONLY and manifest.backup is None:
        raise ValueError(
            f"the cutover reached {manifest.rollback_boundary.value}, which is past the "
            "first durable write, but the manifest carries no backup to restore from"
        )
    _require_placement_matches_boundary(manifest)


def _require_placement_matches_boundary(manifest: MigrationManifest) -> None:
    """Refuse a tier placement that contradicts how far the cutover went.

    Args:
        manifest: The manifest to check.

    Raises:
        ValueError: When a plan-only manifest measures a document nothing
            has written yet, or when a staged cutover reports no
            placement -- either way the manifest would claim a tier
            layout no write produced.
    """
    placed = manifest.tier_placement
    if manifest.rollback_boundary is RollbackBoundary.PLAN_ONLY:
        if placed is not None:
            raise ValueError(
                "a plan-only manifest has written nothing into a tier, but it reports a "
                f"placement of {placed.total_records} records"
            )
        return
    if placed is None:
        raise ValueError(
            f"the cutover reached {manifest.rollback_boundary.value}, which is past the "
            "staged write, but the manifest reports no tier placement"
        )


__all__ = [
    "CONVERTING_DISPOSITIONS",
    "GIT_EVIDENCE_FIELDS",
    "MANIFEST_SCHEMA_VERSION",
    "MILESTONE_TRACK_FIELD",
    "OUTCOME_TRACK_FIELD",
    "UNADDRESSED_TARGETS",
    "VALIDATION_PASS_COUNT",
    "BackupRecord",
    "GitEvidence",
    "GitFact",
    "GitFactKind",
    "ManifestSource",
    "MigrationManifest",
    "OperatorAssignment",
    "RollbackBoundary",
    "RowMapping",
    "SealState",
    "StoreMapping",
    "TargetCensus",
    "TierPlacement",
    "UnresolvedReason",
    "UnresolvedRow",
    "ValidationPass",
    "idempotence_payload",
    "manifest_content_payload",
    "seal_digest_of",
]
