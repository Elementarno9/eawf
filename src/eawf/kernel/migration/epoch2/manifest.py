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
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    Disposition,
    DropProofForm,
)
from eawf.kernel.migration.epoch2.dispositions import StorageTier as DispositionTier
from eawf.kernel.migration.epoch2.lifecycle import DeferralReason
from eawf.kernel.migration.epoch2.rows import COLLECTION_ROW_CONTRACT_INDEX, SourceShape
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    StrictMigrationModel,
    rule_digest,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshotIdentity
from eawf.kernel.migration.epoch2.validation import (
    AliasCollision,
    CorpusIdentity,
    DanglingReference,
    FabricationFinding,
    ImportValidationReport,
)
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

logger = logging.getLogger(__name__)


#: The schema version every epoch-2 manifest carries. Epoch 1's state
#: document versioned as ``1.<n>``; the cutover starts a new series so a
#: reader can tell the two document families apart by one field.
MANIFEST_SCHEMA_VERSION: Final[Literal["2"]] = "2"

#: How many validation passes a sealable manifest records. Two, compared
#: byte for byte: one pass proves the import is clean, the second proves
#: the first was not luck.
VALIDATION_PASS_COUNT = 2

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


class GitFactKind(StrEnum):
    """The git references an epoch-1 row recorded, and nothing more.

    The enum is closed over *facts*: a commit a row named, the identity
    digest taken over it, a candidate release tag. There is deliberately
    no member for an approval or an acceptance, because epoch 1 recorded
    neither and a member for one would invite a later stage to fill it.
    """

    COMMIT = "commit"
    COMMIT_IDENTITY_DIGEST = "commit_identity_digest"
    CANDIDATE_TAG = "candidate_tag"


#: Which source field carries which git fact. The table is the only place
#: the importer looks for git evidence, so a new field has to be declared
#: here before it can reach a manifest.
GIT_EVIDENCE_FIELDS: Mapping[str, GitFactKind] = {
    "commit": GitFactKind.COMMIT,
    "commit_identity_digest": GitFactKind.COMMIT_IDENTITY_DIGEST,
    "candidate_tag": GitFactKind.CANDIDATE_TAG,
}


class UnresolvedReason(StrEnum):
    """Why the cutover cannot place one source row.

    ``NO_CONVERTER`` is a row whose collection has a declared conversion
    that no importer rule implements yet. ``ROW_SCHEMA_INVALID`` is a row
    the census could not read through its declared schema. Both refuse
    the apply; neither is silently skipped, because a skipped row is a
    row the target census can never reconcile against the source.
    """

    NO_CONVERTER = "no_converter"
    ROW_SCHEMA_INVALID = "row_schema_invalid"


class RollbackBoundary(StrEnum):
    """How far a cutover has gone, and therefore what rollback can undo.

    The boundary moves forward through the durable writes of an apply.
    ``CROSSED`` is the one-way door: once a native mutation has been
    accepted against the new generation, restoring the old one would
    discard work nobody recorded elsewhere.
    """

    PLAN_ONLY = "plan_only"
    STAGED = "staged"
    GENERATION_SELECTED = "generation_selected"
    MARKER_WRITTEN = "marker_written"
    CROSSED = "crossed"


class SealState(StrEnum):
    """Whether a manifest is still being assembled or is closed."""

    DRAFT = "draft"
    SEALED = "sealed"


class ManifestSource(StrictMigrationModel):
    """The corpus revision and addressing slots a manifest was built over.

    Attributes:
        snapshot: The digest set naming the pinned epoch-1 revision. It
            carries snapshot-root-relative locators only, so a manifest
            never records where on a machine the corpus was staged.
        identity: The workspace, project and repository slots every
            minted URN is addressed under.
        source_schema_version: The epoch-1 schema version of the document
            the rows were read from.
    """

    snapshot: SourceSnapshotIdentity
    identity: CorpusIdentity
    source_schema_version: Annotated[str, Field(min_length=1, max_length=32)]


class RowMapping(StrictMigrationModel):
    """What the cutover does with one epoch-1 top-level collection.

    Every declared collection has exactly one of these, including the
    collections that hold nothing: a collection absent from the table is
    one whose fate the manifest cannot prove.

    Attributes:
        source_collection: The epoch-1 top-level key.
        disposition: Its declared fate, or ``None`` for a document
            metadata key that carries no rows.
        target_collection: Where its rows land, spelled as the
            disposition table spells it.
        source_tier: The store the disposition table declares for it.
        shape: What the key holds, from the row-contract table.
        source_row_count: How many rows the source holds, or ``None``
            when the source holds no container to count.
        target_row_count: How many records the cutover writes from it. A
            split conversion and a ledger union both legitimately write
            more records than the source holds rows, so this is not
            bounded above by ``source_row_count``.
        unresolved_row_count: How many of its rows the cutover cannot
            place.
        proof_form: Which ``explicit_drop`` proof the collection carries,
            present only when there is nothing to import.
        operator_assignment_count: How many required operator
            assignments its rows raise.
    """

    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    disposition: Disposition | None
    target_collection: Annotated[str, Field(min_length=1, max_length=64)]
    source_tier: DispositionTier
    shape: SourceShape
    source_row_count: Annotated[int, Field(ge=0)] | None
    target_row_count: Annotated[int, Field(ge=0)]
    unresolved_row_count: Annotated[int, Field(ge=0)]
    proof_form: DropProofForm | None
    operator_assignment_count: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _agrees_with_declared_tables(self) -> Self:
        """Pin every declared field against the disposition and row tables.

        Returns:
            The validated mapping.

        Raises:
            ValueError: When the collection is undeclared, or when a
                declared field contradicts the table that owns it. A
                manifest free to restate a disposition is a manifest that
                can disagree with the importer that produced it.
        """
        _require_declared(self)
        _require_counts_consistent(self)
        return self

    @property
    def accounted_row_count(self) -> int:
        """How many of its source rows the cutover has a verdict for."""
        return self.target_row_count + self.unresolved_row_count


class StoreMapping(StrictMigrationModel):
    """Which store tier one epoch-2 collection's imported rows land in.

    Attributes:
        collection: The epoch-2 collection receiving rows.
        tier: The tier that collection is declared at. It is validated
            against the compiled tier table rather than trusted, so a
            manifest can never route bytes somewhere the table forbids.
        row_count: How many records the cutover writes into it.
    """

    collection: Epoch2Collection
    tier: StorageTier
    row_count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _tier_agrees_with_table(self) -> Self:
        """Refuse a tier the compiled tier table does not declare.

        Returns:
            The validated mapping.

        Raises:
            ValueError: When the declared tier is not the one the table
                assigns to this collection.
        """
        declared = tier_for(self.collection)
        if self.tier is not declared:
            raise ValueError(
                f"{self.collection.value} is declared at tier {declared.value}, "
                f"but the manifest routes it to {self.tier.value}"
            )
        return self


class GitFact(StrictMigrationModel):
    """One git reference an epoch-1 row recorded, carried verbatim.

    Attributes:
        address: The source row, as ``<collection>/<id>``.
        field: The source field the reference came from.
        kind: Which git fact it is.
        value: The reference, exactly as the source held it.
    """

    address: Annotated[str, Field(min_length=1, max_length=256)]
    field: Annotated[str, Field(min_length=1, max_length=64)]
    kind: GitFactKind
    value: Annotated[str, Field(min_length=1, max_length=256)]


class GitEvidence(StrictMigrationModel):
    """Every git reference the source recorded, and nothing derived from it.

    Attributes:
        facts: The references, in source order.
        evidence_digest: A digest over the ordered facts, so a second
            plan over one revision can be compared by one string.
    """

    facts: tuple[GitFact, ...]
    evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def of(cls, facts: tuple[GitFact, ...]) -> GitEvidence:
        """Build the evidence block over ``facts``.

        Args:
            facts: The git references read out of the source, in source
                order.

        Returns:
            The evidence block with its digest taken.
        """
        return cls(facts=facts, evidence_digest=rule_digest(_git_payload(facts)))

    @model_validator(mode="after")
    def _digest_covers_facts(self) -> Self:
        """Recompute the digest so a hand-edited fact list cannot pass.

        Returns:
            The validated evidence block.

        Raises:
            ValueError: When the digest does not cover the facts.
        """
        expected = rule_digest(_git_payload(self.facts))
        if self.evidence_digest != expected:
            raise ValueError(
                f"git evidence digest {self.evidence_digest} does not cover its "
                f"{len(self.facts)} facts (expected {expected})"
            )
        return self

    def facts_of_kind(self, kind: GitFactKind) -> tuple[GitFact, ...]:
        """Return every recorded fact of one kind, in source order.

        Args:
            kind: Which git fact to select.

        Returns:
            The matching facts.
        """
        return tuple(fact for fact in self.facts if fact.kind is kind)


class OperatorAssignment(StrictMigrationModel):
    """One field the cutover needs an operator to supply after the import.

    This is not an unresolved row. The record imports; one of its fields
    stays unset and annotated, because the source never recorded the
    fact and inferring it would turn an operator's intent into data.

    Attributes:
        address: The source row, as ``<collection>/<id>``.
        source_collection: The epoch-1 collection the row came from.
        target_collection: Where the record lands, spelled as the
            disposition table spells it.
        target_field: The epoch-2 field awaiting the operator.
        reason: Why the source cannot supply it.
        candidates: The source values an operator may choose between.
    """

    address: Annotated[str, Field(min_length=1, max_length=256)]
    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    target_collection: Annotated[str, Field(min_length=1, max_length=64)]
    target_field: Annotated[str, Field(min_length=1, max_length=64)]
    reason: DeferralReason
    candidates: tuple[Annotated[str, Field(min_length=1)], ...] = ()


class UnresolvedRow(StrictMigrationModel):
    """One source row the cutover cannot place, named so apply refuses.

    Attributes:
        address: The source row, as ``<collection>/<id>``, or the census
            locator when the row failed its schema.
        source_collection: The epoch-1 collection the row came from.
        reason: Why it cannot be placed.
        detail: What a reader needs to act on it.
    """

    address: Annotated[str, Field(min_length=1, max_length=256)]
    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    reason: UnresolvedReason
    detail: Annotated[str, Field(min_length=1, max_length=300)]


class ValidationPass(StrictMigrationModel):
    """What one run of the import validator found.

    The pass is a record, not a gate: it reports collisions, dangling
    references and fabrications rather than refusing them, and
    :class:`MigrationManifest` is where a dirty pass is refused. Keeping
    the refusal in one place is what makes the five manifest rejections
    readable as one rule set.

    Attributes:
        pass_index: Which of the two passes this is, 1-based.
        report_digest: The validator's own digest over its whole report.
        payload_byte_length: How many bytes that digest was taken over,
            so two passes can be compared on size as well as on digest.
        source_rows: How many rows the import writes.
        aliased_rows: How many of those carry a source address to key on.
        minted_records: How many distinct records they resolve to.
        identity_digest: The validator's digest over the whole
            address-to-URN resolution.
        alias_collisions: Every record more than one source row resolves
            to.
        canonical_references: How many canonical references were checked.
        dangling_references: Every reference that resolves to nothing.
        fabrication_findings: Every row the source does not entail.
    """

    pass_index: Annotated[int, Field(ge=1, le=VALIDATION_PASS_COUNT)]
    report_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    payload_byte_length: Annotated[int, Field(ge=1)]
    source_rows: Annotated[int, Field(ge=0)]
    aliased_rows: Annotated[int, Field(ge=0)]
    minted_records: Annotated[int, Field(ge=0)]
    identity_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    alias_collisions: tuple[AliasCollision, ...]
    canonical_references: Annotated[int, Field(ge=0)]
    dangling_references: tuple[DanglingReference, ...]
    fabrication_findings: tuple[FabricationFinding, ...]

    @classmethod
    def of(cls, report: ImportValidationReport, *, pass_index: int) -> ValidationPass:
        """Reduce one validator report to the record a manifest carries.

        The whole report runs to six figures of JSON over a real corpus,
        almost all of it the alias entry per source row. The manifest
        keeps the counts and the digests instead: the digest is what
        proves the two passes agreed, and the entries themselves are
        rebuildable from the snapshot the manifest pins.

        Args:
            report: The validator's report for this pass.
            pass_index: Which pass it is, 1-based.

        Returns:
            The reduced pass record.

        Raises:
            ValidationError: When ``pass_index`` is out of range.
        """
        census = report.aliases.census
        return cls(
            pass_index=pass_index,
            report_digest=report.report_digest,
            payload_byte_length=len(report.digest_payload()),
            source_rows=census.source_rows,
            aliased_rows=census.aliased_rows,
            minted_records=census.minted_records,
            identity_digest=census.identity_digest,
            alias_collisions=census.collisions,
            canonical_references=report.references.canonical_references,
            dangling_references=report.references.dangling,
            fabrication_findings=report.fabrication.findings,
        )

    @property
    def aliases_resolve_injectively(self) -> bool:
        """Whether every keyed source row resolves to a record of its own."""
        return not self.alias_collisions and self.aliased_rows == self.minted_records


class TargetCensus(StrictMigrationModel):
    """How many epoch-2 records the cutover writes, and where.

    Attributes:
        total_rows: Every record the cutover writes.
        planned_rows: The subset written into a collection the epoch-2
            identity grammar does not address, so they never appear in
            ``by_entity_kind``.
        by_entity_kind: Minted records per epoch-2 kind, keyed by the
            kind's URN token.
        by_collection: Records per epoch-2 collection, keyed by the
            collection's storage name.
        census_digest: A digest over every other field.
    """

    total_rows: Annotated[int, Field(ge=0)]
    planned_rows: Annotated[int, Field(ge=0)]
    by_entity_kind: dict[str, Annotated[int, Field(ge=1)]]
    by_collection: dict[str, Annotated[int, Field(ge=1)]]
    census_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def of(
        cls,
        *,
        by_entity_kind: Mapping[str, int],
        by_collection: Mapping[str, int],
        planned_rows: int,
    ) -> TargetCensus:
        """Build the target census from the two groupings of the import.

        Args:
            by_entity_kind: Minted records per epoch-2 kind.
            by_collection: Records per epoch-2 collection.
            planned_rows: How many records are planned outside the
                identity traversal.

        Returns:
            The census with its digest taken.

        Raises:
            ValidationError: When the groupings do not reconcile.
        """
        kinds = dict(sorted(by_entity_kind.items()))
        collections = dict(sorted(by_collection.items()))
        payload = {
            "total_rows": sum(collections.values()),
            "planned_rows": planned_rows,
            "by_entity_kind": kinds,
            "by_collection": collections,
        }
        return cls(
            total_rows=sum(collections.values()),
            planned_rows=planned_rows,
            by_entity_kind=kinds,
            by_collection=collections,
            census_digest=rule_digest(payload),
        )

    @model_validator(mode="after")
    def _groupings_reconcile(self) -> Self:
        """Refuse a census whose two groupings disagree with its totals.

        Returns:
            The validated census.

        Raises:
            ValueError: When the collection grouping does not sum to
                ``total_rows``, when the kind grouping does not sum to
                the addressed remainder, or when the digest does not
                cover the fields.
        """
        by_collection = sum(self.by_collection.values())
        if by_collection != self.total_rows:
            raise ValueError(
                f"the target census totals {self.total_rows} rows but its collection "
                f"grouping sums to {by_collection}"
            )
        addressed = self.total_rows - self.planned_rows
        by_kind = sum(self.by_entity_kind.values())
        if by_kind != addressed:
            raise ValueError(
                f"the target census addresses {addressed} rows but its entity-kind "
                f"grouping sums to {by_kind}"
            )
        expected = rule_digest(
            {
                "total_rows": self.total_rows,
                "planned_rows": self.planned_rows,
                "by_entity_kind": dict(sorted(self.by_entity_kind.items())),
                "by_collection": dict(sorted(self.by_collection.items())),
            }
        )
        if self.census_digest != expected:
            raise ValueError(
                f"target census digest {self.census_digest} does not cover its counts "
                f"(expected {expected})"
            )
        return self


class BackupRecord(StrictMigrationModel):
    """The restore point an apply took before it wrote anything durable.

    Attributes:
        taken_at: When the snapshot set was captured.
        surfaces: One pinned digest per file the restore would rewrite.
        backup_digest: A digest over the whole surface set.
    """

    taken_at: datetime
    surfaces: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...]
    backup_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


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


def _git_payload(facts: tuple[GitFact, ...]) -> list[list[str]]:
    """Return the digestable form of one ordered git-fact list."""
    return [[fact.address, fact.field, fact.kind.value, fact.value] for fact in facts]


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


def _require_declared(mapping: RowMapping) -> None:
    """Pin one row mapping's declarations against the owning tables.

    Args:
        mapping: The row mapping to check.

    Raises:
        ValueError: When the collection is undeclared, or a field
            contradicts the disposition or row-contract table.
    """
    declared = COLLECTION_DISPOSITION_INDEX.get(mapping.source_collection)
    if declared is None:
        raise ValueError(
            f"{mapping.source_collection!r} has no disposition row, so the manifest "
            "cannot state its fate"
        )
    contract = COLLECTION_ROW_CONTRACT_INDEX[mapping.source_collection]
    mismatches: list[str] = []
    if mapping.disposition is not declared.disposition:
        mismatches.append(f"disposition {mapping.disposition} != {declared.disposition}")
    if mapping.target_collection != declared.target_collection:
        mismatches.append(
            f"target_collection {mapping.target_collection!r} != {declared.target_collection!r}"
        )
    if mapping.source_tier is not declared.tier:
        mismatches.append(f"source_tier {mapping.source_tier.value} != {declared.tier.value}")
    if mapping.shape is not contract.shape:
        mismatches.append(f"shape {mapping.shape.value} != {contract.shape.value}")
    if mismatches:
        raise ValueError(
            f"{mapping.source_collection!r} contradicts its declared tables: "
            f"{'; '.join(mismatches)}"
        )


def _require_nothing_written(mapping: RowMapping, *, because: str) -> None:
    """Refuse a mapping that writes records where the tables allow none.

    Args:
        mapping: The row mapping to check.
        because: Why no record may be written, for the message.

    Raises:
        ValueError: When the mapping writes, defers or leaves unresolved
            any record.
    """
    if mapping.accounted_row_count or mapping.operator_assignment_count:
        raise ValueError(
            f"{mapping.source_collection!r} is {because}, so it writes no record, but "
            f"the manifest claims {mapping.target_row_count} target rows, "
            f"{mapping.unresolved_row_count} unresolved rows and "
            f"{mapping.operator_assignment_count} operator assignments"
        )


def _require_counts_consistent(mapping: RowMapping) -> None:
    """Check one row mapping's counts against its disposition and shape.

    The arms are ordered so the most specific claim wins: metadata and a
    derived projection never hold records at all, a collection with a
    drop proof has nothing to import, an explicit drop writes nothing,
    and only then does the accounting rule apply.

    Args:
        mapping: The row mapping to check.

    Raises:
        ValueError: When a count contradicts the mapping's declared fate.
    """
    if mapping.disposition is None:
        if mapping.source_row_count is not None:
            raise ValueError(
                f"{mapping.source_collection!r} is document metadata, which holds no "
                f"rows, but the manifest counts {mapping.source_row_count}"
            )
        _require_nothing_written(mapping, because="document metadata")
        return
    if mapping.disposition is Disposition.DERIVED_PROJECTION:
        _require_nothing_written(mapping, because="a derived projection")
        return
    if mapping.proof_form is not None:
        if mapping.source_row_count:
            raise ValueError(
                f"{mapping.source_collection!r} carries the {mapping.proof_form.value} "
                f"proof form, which asserts nothing to import, but the manifest counts "
                f"{mapping.source_row_count} source rows"
            )
        _require_nothing_written(mapping, because="a collection with no rows")
        return
    if mapping.disposition is Disposition.EXPLICIT_DROP:
        _require_nothing_written(mapping, because="an explicit drop")
        return
    _require_every_source_row_accounted(mapping)


def _require_every_source_row_accounted(mapping: RowMapping) -> None:
    """Refuse a conversion that leaves a source row with no verdict.

    The rule bounds the target count from below, never from above: a
    split conversion writes two records from one row and a ledger union
    writes records the document never held, so a target count larger
    than the source count is a fact about the rules rather than a defect.
    What cannot happen is a source row the manifest neither converts nor
    names unresolved.

    Args:
        mapping: The row mapping to check.

    Raises:
        ValueError: When fewer records are accounted for than the source
            holds.
    """
    required = 1 if mapping.shape is SourceShape.MAPPING else (mapping.source_row_count or 0)
    if mapping.accounted_row_count < required:
        raise ValueError(
            f"{mapping.source_collection!r} holds {required} record(s) to account for, "
            f"but the manifest converts {mapping.target_row_count} and leaves "
            f"{mapping.unresolved_row_count} unresolved"
        )


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
    "UnresolvedReason",
    "UnresolvedRow",
    "ValidationPass",
    "idempotence_payload",
    "manifest_content_payload",
    "seal_digest_of",
]
