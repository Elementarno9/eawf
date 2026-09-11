"""The leaf rows a migration manifest is assembled from.

Each model here answers for itself alone: a row mapping pins its own
declarations against the disposition and row-contract tables, a target
census re-derives its own digest, a git-evidence block re-derives its own.
Nothing in this module reads another row, which is what keeps the
whole-manifest reconciliation in
:mod:`eawf.kernel.migration.epoch2.manifest` readable as one rule set
rather than as validators scattered across the models they happen to sit
on.

:class:`TierPlacement` is the one row a plan cannot carry. Every other
row describes the source or what the cutover intends; the placement
describes what a staged write actually left on disk, so it appears only
once the rollback boundary has moved past ``plan_only``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.dispositions import (
    COLLECTION_DISPOSITION_INDEX,
    Disposition,
    DropProofForm,
)
from eawf.kernel.migration.epoch2.dispositions import StorageTier as DispositionTier
from eawf.kernel.migration.epoch2.lifecycle import DeferralReason
from eawf.kernel.migration.epoch2.rows import COLLECTION_ROW_CONTRACT_INDEX, SourceShape
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshotIdentity
from eawf.kernel.migration.epoch2.validation import (
    AliasCollision,
    CorpusIdentity,
    DanglingReference,
    FabricationFinding,
    ImportValidationReport,
)
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

#: How many validation passes a sealable manifest records. Two, compared
#: byte for byte: one pass proves the import is clean, the second proves
#: the first was not luck.
VALIDATION_PASS_COUNT = 2


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

    @classmethod
    def of(cls, *, taken_at: datetime, surfaces: tuple[str, ...]) -> BackupRecord:
        """Build the restore point over ``surfaces``.

        An empty surface set is a legitimate restore point: it records
        that nothing the write touched existed beforehand, which is a
        different claim from having taken no backup at all.

        Args:
            taken_at: When the surfaces were digested.
            surfaces: One ``<locator>@sha256:<hex>`` entry per file the
                restore would rewrite, in locator order.

        Returns:
            The record with its digest taken.

        Raises:
            ValidationError: When an entry is empty or over-long.
        """
        return cls(
            taken_at=taken_at,
            surfaces=surfaces,
            backup_digest=rule_digest(list(surfaces)),
        )

    @model_validator(mode="after")
    def _digest_covers_surfaces(self) -> Self:
        """Recompute the digest so a hand-edited surface list cannot pass.

        Returns:
            The validated record.

        Raises:
            ValueError: When the digest does not cover the surfaces.
        """
        expected = rule_digest(list(self.surfaces))
        if self.backup_digest != expected:
            raise ValueError(
                f"backup digest {self.backup_digest} does not cover its "
                f"{len(self.surfaces)} surfaces (expected {expected})"
            )
        return self


class TierPlacement(StrictMigrationModel):
    """What a staged write left in each tier, and what the document weighs.

    The claim this row makes is narrow on purpose. It says the document no
    longer holds the records that never change again, so the tree's
    compare-and-swap file is rewritten over work in flight instead of over
    the whole history. It does **not** say the repository got smaller:
    the history moved into append-only ledgers that are themselves
    committed, so the bytes are still there -- they have simply stopped
    being rewritten on every mutation.

    Attributes:
        records_by_tier: How many records landed in each tier, keyed by
            the tier's declared name. A tier that received nothing is
            absent rather than zero.
        document_record_count: How many records the document still holds,
            which is the residual the cutover rewrites per mutation.
        document_byte_length: How many bytes that document occupies.
        ledger_byte_length: How many bytes the append-only ledgers
            occupy, recorded so a reader can see where the history went
            rather than infer that it shrank.
        indexed_collections: The ledger collections whose derived index
            was regenerated, in table order.
        placement_digest: A digest over every other field.
    """

    records_by_tier: dict[str, Annotated[int, Field(ge=1)]]
    document_record_count: Annotated[int, Field(ge=0)]
    document_byte_length: Annotated[int, Field(ge=0)]
    ledger_byte_length: Annotated[int, Field(ge=0)]
    indexed_collections: tuple[Epoch2Collection, ...]
    placement_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def of(
        cls,
        *,
        records_by_tier: Mapping[StorageTier, int],
        document_record_count: int,
        document_byte_length: int,
        ledger_byte_length: int,
        indexed_collections: tuple[Epoch2Collection, ...],
    ) -> TierPlacement:
        """Build the placement row a staged write reports.

        Args:
            records_by_tier: Records written per tier; zero-count tiers
                are dropped so an absent tier means "received nothing".
            document_record_count: Records the document still holds.
            document_byte_length: Bytes the document occupies.
            ledger_byte_length: Bytes the ledgers occupy.
            indexed_collections: Collections whose index was regenerated.

        Returns:
            The placement with its digest taken.

        Raises:
            ValidationError: When a count contradicts another.
        """
        tiers = {tier.value: count for tier, count in sorted(records_by_tier.items()) if count}
        return cls(
            records_by_tier=tiers,
            document_record_count=document_record_count,
            document_byte_length=document_byte_length,
            ledger_byte_length=ledger_byte_length,
            indexed_collections=indexed_collections,
            placement_digest=rule_digest(
                _placement_payload(
                    records_by_tier=tiers,
                    document_record_count=document_record_count,
                    document_byte_length=document_byte_length,
                    ledger_byte_length=ledger_byte_length,
                    indexed_collections=indexed_collections,
                )
            ),
        )

    @property
    def total_records(self) -> int:
        """How many records the staged write placed, across every tier."""
        return sum(self.records_by_tier.values())

    def records_in(self, tier: StorageTier) -> int:
        """Return how many records landed in one tier.

        Args:
            tier: The tier to read.

        Returns:
            Its record count, ``0`` when the tier received nothing.
        """
        return self.records_by_tier.get(tier.value, 0)

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        """Refuse a placement whose tiers, document and digest disagree.

        Returns:
            The validated placement.

        Raises:
            ValueError: When a tier name is not a declared tier, when the
                document count exceeds what the document tier received,
                when a non-empty document claims zero bytes, or when the
                digest does not cover the fields.
            KeyError: Never raised; an undeclared tier name is reported as
                a ``ValueError`` so the message can list the declared set.
        """
        declared = {tier.value for tier in StorageTier}
        unknown = sorted(set(self.records_by_tier) - declared)
        if unknown:
            raise ValueError(
                f"the placement routes records to {', '.join(unknown)}, which no storage "
                f"tier declares ({', '.join(sorted(declared))})"
            )
        in_document = self.records_by_tier.get(StorageTier.DOCUMENT.value, 0)
        if self.document_record_count != in_document:
            raise ValueError(
                f"the placement writes {in_document} records into the document tier but "
                f"reports {self.document_record_count} residual document records"
            )
        if self.document_record_count and not self.document_byte_length:
            raise ValueError(
                f"the document holds {self.document_record_count} records but the "
                "placement reports it as zero bytes"
            )
        expected = rule_digest(
            _placement_payload(
                records_by_tier=self.records_by_tier,
                document_record_count=self.document_record_count,
                document_byte_length=self.document_byte_length,
                ledger_byte_length=self.ledger_byte_length,
                indexed_collections=self.indexed_collections,
            )
        )
        if self.placement_digest != expected:
            raise ValueError(
                f"placement digest {self.placement_digest} does not cover its counts "
                f"(expected {expected})"
            )
        return self


def _placement_payload(
    *,
    records_by_tier: Mapping[str, int],
    document_record_count: int,
    document_byte_length: int,
    ledger_byte_length: int,
    indexed_collections: tuple[Epoch2Collection, ...],
) -> dict[str, object]:
    """Return the payload :attr:`TierPlacement.placement_digest` covers."""
    return {
        "records_by_tier": dict(sorted(records_by_tier.items())),
        "document_record_count": document_record_count,
        "document_byte_length": document_byte_length,
        "ledger_byte_length": ledger_byte_length,
        "indexed_collections": [collection.value for collection in indexed_collections],
    }


def _git_payload(facts: tuple[GitFact, ...]) -> list[list[str]]:
    """Return the digestable form of one ordered git-fact list."""
    return [[fact.address, fact.field, fact.kind.value, fact.value] for fact in facts]


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


__all__ = [
    "GIT_EVIDENCE_FIELDS",
    "VALIDATION_PASS_COUNT",
    "BackupRecord",
    "GitEvidence",
    "GitFact",
    "GitFactKind",
    "ManifestSource",
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
]
