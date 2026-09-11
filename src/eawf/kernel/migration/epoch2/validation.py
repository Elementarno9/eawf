"""Checking a staged import before any manifest is sealed.

The importer rules each decide one row correctly. Nothing yet checks
that the rows they produced form a corpus somebody can read: that every
source identifier still resolves, that it resolves to exactly one record,
that no reference points at a record the import never wrote, and that no
record appeared without a source row behind it.

Those are three separate questions and this module answers them over one
traversal. The traversal reduces the whole plan to :class:`StagedImport`
-- one :class:`ImportedRow` per record the import writes -- and each
census reads that reduction rather than re-walking the plan, so the three
answers are guaranteed to describe the same import.

The distinction the reference census turns on is which strings are
references at all. A canonical reference names an epoch-2 record and must
resolve. A legacy-reference string is a source field with no epoch-2
referent -- a claim-session id, an audit id -- carried verbatim on the
envelope. It is counted, its resolution is reported, and it is never
dangling: there is no canonical edge to dangle, which is precisely why
carrying it invents nothing.

Reporting and refusing are separate, as everywhere in this package. A
census names every problem it found; :meth:`ImportValidationReport.require_clean`
is what turns a problem into a refusal, so an operator sees the whole
picture rather than the first defect.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Self

from pydantic import Field, model_validator

from eawf.kernel.identity.alias import LegacyAliasEntry, LegacyAliasIndex, LegacyAliasKey
from eawf.kernel.identity.keys import (
    EntityKeyAllocator,
    EntityKeyTransaction,
    EntityKind,
    project_code_of,
    validate_symbol_key,
)
from eawf.kernel.identity.urn import format_qualified_urn
from eawf.kernel.migration.epoch2.allowlist import load_legacy_symbol_allowlist
from eawf.kernel.migration.epoch2.envelopes import AUDIT_LEDGER, ImportedLedgerRow, LegacyEnvelope
from eawf.kernel.migration.epoch2.errors import MigrationFabricationDetectedError
from eawf.kernel.migration.epoch2.lifecycle import (
    BATCH_HEAD_BINDING_FIELD,
    LIFECYCLE_FIELD_ROUTES,
    MILESTONE_ACCEPTANCE_FIELDS,
    ImportedLifecycleRecord,
    LifecycleFieldDisposition,
    LifecycleTarget,
)
from eawf.kernel.migration.epoch2.measurements import TASK_REF_FIELD, ImportedMeasurement
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.registry import mapping_rule_index
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    StrictMigrationModel,
    canonical_json,
    rule_digest,
)
from eawf.kernel.migration.epoch2.runs import (
    ATTEMPT_RUN_ID_SEPARATOR,
    UNCLASSIFIED_RUN_STATUS,
    MintedRun,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.state.epoch2.values import EntityOrigin

logger = logging.getLogger(__name__)


#: Which epoch-2 kind each lifecycle target is addressed as.
LIFECYCLE_ENTITY_KINDS: Mapping[LifecycleTarget, EntityKind] = MappingProxyType(
    {
        LifecycleTarget.MILESTONE: EntityKind.MILESTONE,
        LifecycleTarget.BATCH: EntityKind.BATCH,
        LifecycleTarget.TASK: EntityKind.TASK,
    }
)

#: Every epoch-2 field that names another epoch-2 record, and the kind it
#: names. A reference here must resolve to a record the import wrote; a
#: reference to a kind no stage of the import mints is dangling, because
#: nothing was written for it to point at.
CANONICAL_REFERENCE_KINDS: Mapping[str, EntityKind] = MappingProxyType(
    {
        "primary_track_ref": EntityKind.TRACK,
        "required_batch_refs": EntityKind.BATCH,
        "milestone_ref": EntityKind.MILESTONE,
        "task_refs": EntityKind.TASK,
        "batch_ref": EntityKind.BATCH,
    }
)

#: Source fields carried as strings on the legacy envelope, and the
#: source population each one names. None of them is a canonical
#: reference, so none of them can dangle; the census reports how many
#: resolve so an operator can see what the source actually recorded.
LEGACY_REFERENCE_POPULATIONS: Mapping[str, str] = MappingProxyType(
    {
        "audit_id": "audits",
        "claim_session_id": "agent_sessions",
        "estimate_id": "estimates",
        "worktree_id": "worktrees",
    }
)

#: The epoch-1 collections each legacy-reference population is read from.
#: ``audits`` is the union of the document collection and the store
#: ledger, which is the same population the audit reconciliation uses.
POPULATION_COLLECTIONS: tuple[str, ...] = ("agent_sessions", "estimates", "worktrees")

#: How a Run minted from a resolving claim is addressed. An attempt-minted
#: Run already has an address of its own; a claim-minted Run has one per
#: wave, and it must not collide with the wave's own source address.
CLAIM_RUN_ADDRESS_SUFFIX = "#claim"

#: The epoch-1 read surfaces a minted Run is attributed to.
ATTEMPT_RUN_SOURCE_KIND = "waves.sessions"
CLAIM_RUN_SOURCE_KIND = "waves.claim_session_id"

#: The Run field that may not carry an outcome without a bound report.
RUN_STATUS_FIELD = "status"


def _native_reference_targets() -> frozenset[str]:
    """Return every epoch-2 reference field the lifecycle routes write."""
    return frozenset(
        route.target_key
        for table in LIFECYCLE_FIELD_ROUTES.values()
        for route in table.values()
        if route.disposition is LifecycleFieldDisposition.NATIVE_CONVERSION
        and (route.target_key.endswith("_ref") or route.target_key.endswith("_refs"))
    )


def _native_legacy_targets() -> frozenset[str]:
    """Return every legacy-envelope key the lifecycle routes write."""
    return frozenset(
        route.target_key
        for table in LIFECYCLE_FIELD_ROUTES.values()
        for route in table.values()
        if route.disposition is LifecycleFieldDisposition.LEGACY_REF
    )


def _assert_reference_tables_total() -> None:
    """Fail import unless both reference tables cover the routes exactly.

    Raises:
        ValueError: When a route writes a reference field no table
            classifies, which would let the field escape validation
            entirely, or when a table names a field no route writes.
    """
    routed = _native_reference_targets()
    declared = frozenset(CANONICAL_REFERENCE_KINDS)
    if routed != declared:
        raise ValueError(
            f"canonical reference table is not total: unclassified={sorted(routed - declared)}, "
            f"unknown={sorted(declared - routed)}"
        )
    unknown = frozenset(LEGACY_REFERENCE_POPULATIONS) - _native_legacy_targets()
    if unknown:
        raise ValueError(f"legacy reference table names unrouted fields: {sorted(unknown)}")


_assert_reference_tables_total()


class FabricationReason(StrEnum):
    """Why one row of a staged import is not entailed by the source."""

    NO_SOURCE_LOCATOR = "no_source_locator"
    OUTCOME_WITHOUT_BOUND_REPORT = "run_outcome_without_bound_report"
    ACCEPTANCE_WITHOUT_SOURCE_PROOF = "acceptance_without_source_proof"
    HEAD_BINDING_WITHOUT_SOURCE_PROOF = "head_binding_without_source_proof"


class UnsupportedClaim(StrictMigrationModel):
    """One epoch-2 field a row filled without a source fact behind it.

    Attributes:
        field: The epoch-2 field that was filled.
        reason: Why the source does not entail it.
    """

    field: Annotated[str, Field(min_length=1)]
    reason: FabricationReason


class SourceReference(StrictMigrationModel):
    """One canonical reference held by an imported row.

    Attributes:
        field: The epoch-2 field carrying the reference.
        referent_kind: The kind of record the reference names.
        value: The source identifier of the referent.
    """

    field: Annotated[str, Field(min_length=1)]
    referent_kind: EntityKind
    value: Annotated[str, Field(min_length=1)]


class LegacyString(StrictMigrationModel):
    """One legacy-reference string carried on an imported row.

    Attributes:
        field: The source field the string came from.
        population: The source population the string names.
        value: The string, verbatim.
        resolves: Whether that population really holds it. A ``False``
            here is a fact about the source, never a dangling edge.
    """

    field: Annotated[str, Field(min_length=1)]
    population: Annotated[str, Field(min_length=1)]
    value: Annotated[str, Field(min_length=1)]
    resolves: bool


class ImportedRow(StrictMigrationModel):
    """One record a staged import writes, reduced to what validation reads.

    Attributes:
        address: Where the row sits in the import, as
            ``<source collection>/<source id>``, which is what every
            census names a finding by.
        source_kind: The epoch-1 read surface the row came from, or
            ``None`` when the row carries a native origin and therefore
            no source row at all.
        source_id: The row's own key on that surface, or ``None``.
        entity_kind: The epoch-2 kind the row is addressed as.
        identifier: The identity the row's canonical key is minted per.
            Two rows sharing one identifier are one record, which is what
            makes a collision visible instead of silently duplicating.
        references: The canonical references the row holds.
        legacy_strings: The legacy-reference strings the row carries.
        unsupported_claims: Epoch-2 fields the row filled that the source
            does not entail.
    """

    address: Annotated[str, Field(min_length=1)]
    source_kind: Annotated[str, Field(min_length=1)] | None
    source_id: Annotated[str, Field(min_length=1)] | None
    entity_kind: EntityKind
    identifier: Annotated[str, Field(min_length=1)]
    references: tuple[SourceReference, ...] = ()
    legacy_strings: tuple[LegacyString, ...] = ()
    unsupported_claims: tuple[UnsupportedClaim, ...] = ()

    @property
    def has_source_locator(self) -> bool:
        """Whether the row can cite the source row its disposition read."""
        return self.source_kind is not None and self.source_id is not None


class CorpusIdentity(StrictMigrationModel):
    """The three slots every minted epoch-2 URN is addressed under.

    Epoch 1 recorded a project code and nothing else about addressing, so
    the workspace and repository the corpus lands in are an operator
    decision the cutover supplies rather than a fact the importer can
    read. They are required here for exactly that reason: a default would
    be a guess wearing a value's clothes.

    Attributes:
        workspace_key: The addressing workspace.
        project_key: The addressing project.
        repository_key: The addressing repository.
    """

    workspace_key: Annotated[str, Field(min_length=1)]
    project_key: Annotated[str, Field(min_length=1)]
    repository_key: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def _slots_are_symbol_keys(self) -> Self:
        """Refuse a slot that is not a bounded uppercase symbol.

        Raises:
            IdentityError: ``symbol_key_invalid`` for the offending slot.
        """
        validate_symbol_key(self.workspace_key, slot="workspace")
        validate_symbol_key(self.project_key, slot="project")
        validate_symbol_key(self.repository_key, slot="repository")
        return self

    @property
    def project_code(self) -> str:
        """The bare project code a task key of this project carries."""
        return project_code_of(self.project_key)

    def urn_for(self, *, kind: EntityKind, entity_key: str) -> str:
        """Return the qualified URN addressing ``entity_key`` of ``kind``.

        Args:
            kind: The entity kind being addressed.
            entity_key: The minted public key.

        Returns:
            The canonical URN string.

        Raises:
            IdentityError: Any slot or key rule is broken.
        """
        return format_qualified_urn(
            workspace_key=self.workspace_key,
            project_key=self.project_key,
            repository_key=self.repository_key,
            kind=kind,
            entity_key=entity_key,
        )


class StagedImport(StrictMigrationModel):
    """A whole staged import reduced to the rows validation reads.

    Attributes:
        source_schema_version: The epoch-1 schema version every alias key
            is qualified by.
        project_code: The project code every alias key is qualified by.
        rows: One row per record the import writes, in a deterministic
            order: lifecycle records, then Runs, then measurements, then
            envelopes and ledger rows.
    """

    source_schema_version: Annotated[str, Field(min_length=1)]
    project_code: Annotated[str, Field(min_length=1)]
    rows: tuple[ImportedRow, ...]

    @classmethod
    def reduce(cls, *, plan: CorpusImportPlan, snapshot: SourceSnapshot) -> StagedImport:
        """Reduce ``plan`` to the rows the censuses read.

        Args:
            plan: The staged import.
            snapshot: The frozen corpus the plan was built from, read for
                the populations a legacy-reference string resolves in.

        Returns:
            The reduction, with rows in import order.

        Raises:
            MigrationSourceUnreadableError: When the snapshot holds no
                audit ledger, which the audit population needs.
            ValidationError: When a reduced row violates its contract.
        """
        populations = source_populations(snapshot)
        rows: list[ImportedRow] = [
            *(
                _lifecycle_row(record=record, populations=populations)
                for record in plan.lifecycle.records
            ),
            *(_run_row(run) for run in plan.lifecycle.minted_runs()),
            *(_measurement_row(row) for row in plan.measurements.measurements),
            *(_envelope_row(row) for row in plan.envelopes.envelopes),
            *(_envelope_row(row) for row in plan.envelopes.ledger_rows),
        ]
        return cls(
            source_schema_version=plan.lifecycle.source_index.source_schema_version,
            project_code=_project_code(snapshot),
            rows=tuple(rows),
        )

    def identifiers(self) -> frozenset[tuple[EntityKind, str]]:
        """Return the identity of every record the import wrote."""
        return frozenset((row.entity_kind, row.identifier) for row in self.rows)


def _project_code(snapshot: SourceSnapshot) -> str:
    """Return the project code the epoch-1 document records.

    Args:
        snapshot: The frozen epoch-1 corpus.

    Returns:
        The recorded project code.

    Raises:
        MigrationFabricationDetectedError: When the document records
            none. Every alias key is qualified by the project the source
            row belonged to, and a guessed code would make two projects'
            rows collide in one index.
    """
    project = snapshot.document.get("project")
    code = project.get("code") if isinstance(project, Mapping) else None
    if not isinstance(code, str) or not code:
        raise MigrationFabricationDetectedError(
            "the epoch-1 document records no project code to qualify alias keys with"
        )
    return code


def source_populations(snapshot: SourceSnapshot) -> Mapping[str, frozenset[str]]:
    """Return the source populations a legacy-reference string resolves in.

    Args:
        snapshot: The frozen epoch-1 corpus.

    Returns:
        One id set per population named by
        :data:`LEGACY_REFERENCE_POPULATIONS`.

    Raises:
        MigrationSourceUnreadableError: When the snapshot holds no audit
            ledger. A missing ledger is not an empty one, and treating it
            as empty would report resolving audit ids as unresolved.
    """
    document = snapshot.document
    populations = {
        collection: frozenset(_collection_keys(document, collection))
        for collection in POPULATION_COLLECTIONS
    }
    ledger_ids = frozenset(
        row["id"]
        for row in snapshot.ledger(AUDIT_LEDGER)
        if isinstance(row.get("id"), str) and row["id"]
    )
    populations["audits"] = frozenset(_collection_keys(document, "audits")) | ledger_ids
    return MappingProxyType(populations)


def _collection_keys(document: Mapping[str, Any], collection: str) -> tuple[str, ...]:
    """Return the row keys of one collection, or nothing when it holds none."""
    value = document.get(collection)
    if not isinstance(value, Mapping):
        return ()
    return tuple(str(key) for key in value if key)


def _references_of(record: ImportedLifecycleRecord) -> tuple[SourceReference, ...]:
    """Return every canonical reference one lifecycle record holds."""
    references: list[SourceReference] = []
    for field, kind in CANONICAL_REFERENCE_KINDS.items():
        value = record.record.get(field)
        for item in _reference_values(value):
            references.append(SourceReference(field=field, referent_kind=kind, value=item))
    return tuple(references)


def _reference_values(value: Any) -> Iterator[str]:
    """Yield the non-empty reference strings held by one field value."""
    if isinstance(value, str):
        if value:
            yield value
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item:
                yield item


def _legacy_strings_of(
    *,
    record: ImportedLifecycleRecord,
    populations: Mapping[str, frozenset[str]],
) -> tuple[LegacyString, ...]:
    """Return every legacy-reference string one lifecycle record carries."""
    carried: list[LegacyString] = []
    for field, population in LEGACY_REFERENCE_POPULATIONS.items():
        value = record.legacy_refs.get(field)
        if not isinstance(value, str) or not value:
            continue
        carried.append(
            LegacyString(
                field=field,
                population=population,
                value=value,
                resolves=value in populations.get(population, frozenset()),
            )
        )
    return tuple(carried)


def _unsupported_claims_of(record: ImportedLifecycleRecord) -> tuple[UnsupportedClaim, ...]:
    """Return every epoch-2 field one lifecycle record filled unsupported.

    Epoch 1 never recorded a Milestone's acceptance proof or a Batch's
    head binding, so either appearing on an imported record means the
    importer supplied it rather than read it.
    """
    claims: list[UnsupportedClaim] = []
    if record.target is LifecycleTarget.MILESTONE:
        claims.extend(
            UnsupportedClaim(field=field, reason=FabricationReason.ACCEPTANCE_WITHOUT_SOURCE_PROOF)
            for field in MILESTONE_ACCEPTANCE_FIELDS
            if field in record.record
        )
    if record.target is LifecycleTarget.BATCH and BATCH_HEAD_BINDING_FIELD in record.record:
        claims.append(
            UnsupportedClaim(
                field=BATCH_HEAD_BINDING_FIELD,
                reason=FabricationReason.HEAD_BINDING_WITHOUT_SOURCE_PROOF,
            )
        )
    return tuple(claims)


def _source_slots(origin: EntityOrigin) -> tuple[str | None, str | None]:
    """Return the source surface and row id an origin cites, if it cites one."""
    if origin.kind != "legacy":
        return None, None
    return origin.source_kind, origin.source_id


def _address(*, source_kind: str | None, source_id: str | None, fallback: str) -> str:
    """Return the address a census names a row by."""
    if source_kind is None or source_id is None:
        return fallback
    return f"{source_kind}/{source_id}"


def _lifecycle_row(
    *,
    record: ImportedLifecycleRecord,
    populations: Mapping[str, frozenset[str]],
) -> ImportedRow:
    """Reduce one imported lifecycle record."""
    source_kind, source_id = _source_slots(record.origin)
    identifier = source_id if source_id is not None else record.source_collection
    return ImportedRow(
        address=_address(
            source_kind=source_kind,
            source_id=source_id,
            fallback=f"{record.source_collection}/{record.target.value}",
        ),
        source_kind=source_kind,
        source_id=source_id,
        entity_kind=LIFECYCLE_ENTITY_KINDS[record.target],
        identifier=identifier,
        references=_references_of(record),
        legacy_strings=_legacy_strings_of(record=record, populations=populations),
        unsupported_claims=_unsupported_claims_of(record),
    )


def run_source_address(run: MintedRun) -> str:
    """Return the epoch-1 address one minted Run came from.

    A Run is not a source row, so it has no key of its own in the
    document. Its address is the wave plus the attempt entry that minted
    it, which is unique per wave and cannot collide with the wave's own
    source address.

    Args:
        run: The minted Run.

    Returns:
        The address, as ``<wave>#attempt-<key>`` or ``<wave>#claim``.
    """
    if run.attempt_key is None:
        return f"{run.wave_id}{CLAIM_RUN_ADDRESS_SUFFIX}"
    return f"{run.wave_id}{ATTEMPT_RUN_ID_SEPARATOR}{run.attempt_key}"


def _run_row(run: MintedRun) -> ImportedRow:
    """Reduce one minted Run."""
    source_kind = CLAIM_RUN_SOURCE_KIND if run.attempt_key is None else ATTEMPT_RUN_SOURCE_KIND
    address = run_source_address(run)
    claims: tuple[UnsupportedClaim, ...] = ()
    if run.status != UNCLASSIFIED_RUN_STATUS and run.binding is None:
        claims = (
            UnsupportedClaim(
                field=RUN_STATUS_FIELD,
                reason=FabricationReason.OUTCOME_WITHOUT_BOUND_REPORT,
            ),
        )
    return ImportedRow(
        address=f"{source_kind}/{address}",
        source_kind=source_kind,
        source_id=address,
        entity_kind=EntityKind.RUN,
        identifier=address,
        references=(
            SourceReference(field=TASK_REF_FIELD, referent_kind=EntityKind.TASK, value=run.wave_id),
        ),
        unsupported_claims=claims,
    )


def _measurement_row(measurement: ImportedMeasurement) -> ImportedRow:
    """Reduce one re-pointed measurement."""
    source_kind, source_id = _source_slots(measurement.origin)
    identifier = f"{measurement.source_collection}/{measurement.map_key}"
    references: tuple[SourceReference, ...] = ()
    if measurement.task_ref is not None:
        references = (
            SourceReference(
                field=TASK_REF_FIELD,
                referent_kind=EntityKind.TASK,
                value=measurement.task_ref,
            ),
        )
    return ImportedRow(
        address=_address(source_kind=source_kind, source_id=source_id, fallback=identifier),
        source_kind=source_kind,
        source_id=source_id,
        entity_kind=EntityKind.LEGACY_RECORD,
        identifier=identifier,
        references=references,
    )


def _envelope_row(row: LegacyEnvelope | ImportedLedgerRow) -> ImportedRow:
    """Reduce one legacy envelope or ledger row."""
    source_kind, source_id = _source_slots(row.origin)
    return ImportedRow(
        address=_address(source_kind=source_kind, source_id=source_id, fallback=row.alias),
        source_kind=source_kind,
        source_id=source_id,
        entity_kind=EntityKind.LEGACY_RECORD,
        identifier=row.alias,
    )


class MintedIdentity(StrictMigrationModel):
    """The canonical identity one imported record is addressed by.

    Attributes:
        entity_kind: The epoch-2 kind.
        entity_key: The minted public key.
        urn: The qualified URN the key renders under.
    """

    entity_kind: EntityKind
    entity_key: Annotated[str, Field(min_length=1)]
    urn: Annotated[str, Field(min_length=1)]


def mint_identities(
    *,
    staged: StagedImport,
    identity: CorpusIdentity,
) -> tuple[MintedIdentity, ...]:
    """Mint one canonical identity per distinct record of ``staged``.

    Keys are minted per identifier rather than per row, because two rows
    that name one identity are one record. A wave and a backlog row that
    share an id are the same Task -- promotion never changes identity --
    so they resolve to one URN and the alias census sees the collision
    instead of the import silently writing the record twice.

    Args:
        staged: The reduced import.
        identity: The addressing slots minted URNs are rendered under.

    Returns:
        One identity per row of ``staged``, in row order; rows sharing an
        identifier share an identity.

    Raises:
        IdentityError: ``kind_not_allocatable`` when a row claims a kind
            whose keys are supplied rather than minted, or
            ``key_space_saturated`` when a family runs out of ordinals.
    """
    minted: dict[tuple[EntityKind, str], MintedIdentity] = {}
    order: list[MintedIdentity] = []
    with ExitStack() as stack:
        transactions: dict[EntityKind, EntityKeyTransaction] = {}
        for row in staged.rows:
            slot = (row.entity_kind, row.identifier)
            existing = minted.get(slot)
            if existing is None:
                transaction = transactions.get(row.entity_kind)
                if transaction is None:
                    transaction = stack.enter_context(
                        _allocator(kind=row.entity_kind, identity=identity).transaction()
                    )
                    transactions[row.entity_kind] = transaction
                entity_key = transaction.allocate()
                existing = MintedIdentity(
                    entity_kind=row.entity_kind,
                    entity_key=entity_key,
                    urn=identity.urn_for(kind=row.entity_kind, entity_key=entity_key),
                )
                minted[slot] = existing
            order.append(existing)
    return tuple(order)


def _allocator(*, kind: EntityKind, identity: CorpusIdentity) -> EntityKeyAllocator:
    """Open the key counter of ``kind`` for the importing workspace."""
    return EntityKeyAllocator(
        workspace_key=identity.workspace_key,
        kind=kind,
        project_code=identity.project_code if kind is EntityKind.TASK else None,
    )


class AliasCollision(StrictMigrationModel):
    """Two or more source rows that resolve to one epoch-2 record.

    Attributes:
        target_urn: The record they all resolve to.
        source_addresses: The colliding source rows, in import order.
    """

    target_urn: Annotated[str, Field(min_length=1)]
    source_addresses: tuple[str, ...]


class AliasCensus(StrictMigrationModel):
    """How the source identifiers of a staged import resolve.

    Attributes:
        source_rows: How many rows carry a source address to key on.
        aliased_rows: How many of those produced an alias entry.
        minted_records: How many distinct records they resolve to. Equal
            to ``aliased_rows`` exactly when resolution is injective.
        by_entity_kind: Distinct minted records per epoch-2 kind, keyed
            by the kind's URN token.
        collisions: Every record more than one source row resolves to.
        identity_digest: A digest over the ordered address-to-URN pairs,
            so the report digest covers the whole resolution and not just
            its counts.
    """

    source_rows: Annotated[int, Field(ge=0)]
    aliased_rows: Annotated[int, Field(ge=0)]
    minted_records: Annotated[int, Field(ge=0)]
    by_entity_kind: dict[str, int]
    collisions: tuple[AliasCollision, ...]
    identity_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @property
    def is_injective(self) -> bool:
        """Whether every source row resolves to a record of its own."""
        return not self.collisions and self.aliased_rows == self.minted_records


class ImportAliasIndex(StrictMigrationModel):
    """Every epoch-1 identifier of a staged import, and what it resolves to.

    Attributes:
        source_schema_version: The schema version every key is qualified
            by.
        project_code: The project code every key is qualified by.
        entries: One alias entry per source row that carries an address.
        census: How the resolution came out.
    """

    source_schema_version: Annotated[str, Field(min_length=1)]
    project_code: Annotated[str, Field(min_length=1)]
    entries: tuple[LegacyAliasEntry, ...]
    census: AliasCensus

    @classmethod
    def build(cls, *, staged: StagedImport, identity: CorpusIdentity) -> ImportAliasIndex:
        """Mint the identities of ``staged`` and index them by source row.

        Args:
            staged: The reduced import.
            identity: The addressing slots minted URNs are rendered under.

        Returns:
            The index, including any collision it found. Reporting a
            collision is this method's job; refusing one is
            :meth:`resolvable_index`.

        Raises:
            IdentityError: A minted key or URN violates the grammar.
            ValidationError: A reduced row cannot be keyed.
        """
        minted = mint_identities(staged=staged, identity=identity)
        pairs = tuple(zip(staged.rows, minted, strict=True))
        entries = _alias_entries(pairs=pairs, staged=staged)
        return cls(
            source_schema_version=staged.source_schema_version,
            project_code=staged.project_code,
            entries=entries,
            census=_alias_census(pairs=pairs, aliased_rows=len(entries)),
        )

    def key_for(self, *, source_kind: str, source_id: str) -> LegacyAliasKey:
        """Return the alias key one epoch-1 source row is addressed by.

        Args:
            source_kind: The epoch-1 read surface the row came from.
            source_id: The row's own key on that surface.

        Returns:
            The alias key.

        Raises:
            ValidationError: When either part is empty.
        """
        return LegacyAliasKey(
            source_schema_version=self.source_schema_version,
            source_kind=source_kind,
            source_id=source_id,
            source_project_code=self.project_code,
        )

    def resolvable_index(self) -> LegacyAliasIndex:
        """Return the resolution index, refusing one that cannot resolve.

        Returns:
            The alias index every epoch-1 identifier resolves through.

        Raises:
            IdentityError: ``alias_key_duplicate`` when one source row is
                keyed twice, ``alias_target_not_injective`` when two
                source rows resolve to one record, or
                ``alias_chain_forbidden`` when a target is itself an
                alias source address.
        """
        return LegacyAliasIndex.build(self.entries)

    def refuse_mutation(self, *, source_kind: str, source_id: str) -> None:
        """Refuse a native mutation addressed by an epoch-1 identifier.

        This is the guard a native mutator calls before it writes: an
        epoch-1 identifier names history, history is read-only, and the
        refusal carries the canonical URN to read instead so the caller
        does not have to look it up.

        Args:
            source_kind: The epoch-1 read surface the identifier names.
            source_id: The row's own key on that surface.

        Raises:
            IdentityError: ``legacy_identity_read_only`` when the
                identifier is an alias, or one of the resolution
                rejections when the index itself cannot resolve.
        """
        self.resolvable_index().assert_mutable(
            self.key_for(source_kind=source_kind, source_id=source_id)
        )


def _alias_entries(
    *,
    pairs: tuple[tuple[ImportedRow, MintedIdentity], ...],
    staged: StagedImport,
) -> tuple[LegacyAliasEntry, ...]:
    """Key every row of ``pairs`` that carries a source address.

    A row with no source address is left out rather than keyed under a
    placeholder: it is already a fabrication finding, and an invented key
    would make it resolvable and hide that.
    """
    entries: list[LegacyAliasEntry] = []
    for row, mint in pairs:
        if row.source_kind is None or row.source_id is None:
            continue
        entries.append(
            LegacyAliasEntry(
                key=LegacyAliasKey(
                    source_schema_version=staged.source_schema_version,
                    source_kind=row.source_kind,
                    source_id=row.source_id,
                    source_project_code=staged.project_code,
                ),
                target=mint.urn,
            )
        )
    return tuple(entries)


def _alias_census(
    *,
    pairs: tuple[tuple[ImportedRow, MintedIdentity], ...],
    aliased_rows: int,
) -> AliasCensus:
    """Count how the minted identities of ``pairs`` came out."""
    claimants: dict[str, list[str]] = {}
    by_kind: dict[str, set[str]] = {}
    for row, mint in pairs:
        claimants.setdefault(mint.urn, []).append(row.address)
        by_kind.setdefault(mint.entity_kind.value, set()).add(mint.urn)
    return AliasCensus(
        source_rows=len(pairs),
        aliased_rows=aliased_rows,
        minted_records=len(claimants),
        by_entity_kind={kind: len(urns) for kind, urns in sorted(by_kind.items())},
        collisions=tuple(
            AliasCollision(target_urn=urn, source_addresses=tuple(addresses))
            for urn, addresses in sorted(claimants.items())
            if len(addresses) > 1
        ),
        identity_digest=rule_digest([[row.address, mint.urn] for row, mint in pairs]),
    )


class DanglingReference(StrictMigrationModel):
    """One canonical reference that names a record the import never wrote.

    Attributes:
        source_address: The row holding the reference.
        field: The epoch-2 field carrying it.
        referent_kind: The kind of record it claims to name.
        reference: The identifier that resolves to nothing.
    """

    source_address: Annotated[str, Field(min_length=1)]
    field: Annotated[str, Field(min_length=1)]
    referent_kind: EntityKind
    reference: Annotated[str, Field(min_length=1)]


class LegacyReferenceCount(StrictMigrationModel):
    """How many strings of one legacy-reference field the import carries.

    Attributes:
        field: The source field.
        population: The source population the field names.
        carried: How many rows carry a non-empty string.
        resolving: How many of those the population really holds. The
            remainder is not dangling: the string is the recorded fact
            and no canonical edge was ever written for it.
    """

    field: Annotated[str, Field(min_length=1)]
    population: Annotated[str, Field(min_length=1)]
    carried: Annotated[int, Field(ge=0)]
    resolving: Annotated[int, Field(ge=0)]


class ReferenceCensus(StrictMigrationModel):
    """How the references of a staged import resolve.

    Attributes:
        canonical_references: How many canonical references were checked.
        dangling: Every one that resolves to nothing.
        legacy_strings: One row per legacy-reference field, in field
            order.
    """

    canonical_references: Annotated[int, Field(ge=0)]
    dangling: tuple[DanglingReference, ...]
    legacy_strings: tuple[LegacyReferenceCount, ...]

    @classmethod
    def build(cls, staged: StagedImport) -> ReferenceCensus:
        """Resolve every reference of ``staged`` against its own rows.

        Args:
            staged: The reduced import.

        Returns:
            The census, naming every dangling canonical reference.
        """
        population = staged.identifiers()
        return cls(
            canonical_references=sum(len(row.references) for row in staged.rows),
            dangling=tuple(
                DanglingReference(
                    source_address=row.address,
                    field=reference.field,
                    referent_kind=reference.referent_kind,
                    reference=reference.value,
                )
                for row in staged.rows
                for reference in row.references
                if (reference.referent_kind, reference.value) not in population
            ),
            legacy_strings=_legacy_reference_counts(staged),
        )

    def count_for(self, field: str) -> LegacyReferenceCount:
        """Return the legacy-reference count of one field.

        Args:
            field: The source field to look up.

        Returns:
            Its count row.

        Raises:
            KeyError: When ``field`` is not a declared legacy-reference
                field.
        """
        for row in self.legacy_strings:
            if row.field == field:
                return row
        raise KeyError(field)


def _legacy_reference_counts(staged: StagedImport) -> tuple[LegacyReferenceCount, ...]:
    """Count the legacy-reference strings of ``staged``, field by field."""
    carried: dict[str, int] = dict.fromkeys(LEGACY_REFERENCE_POPULATIONS, 0)
    resolving: dict[str, int] = dict.fromkeys(LEGACY_REFERENCE_POPULATIONS, 0)
    for row in staged.rows:
        for string in row.legacy_strings:
            carried[string.field] += 1
            resolving[string.field] += int(string.resolves)
    return tuple(
        LegacyReferenceCount(
            field=field,
            population=population,
            carried=carried[field],
            resolving=resolving[field],
        )
        for field, population in sorted(LEGACY_REFERENCE_POPULATIONS.items())
    )


class FabricationFinding(StrictMigrationModel):
    """One row of a staged import the source does not entail.

    Attributes:
        address: The row, named the way every census names one.
        reason: Why the source does not entail it.
        field: The epoch-2 field at fault, or ``None`` when the whole row
            is the finding.
    """

    address: Annotated[str, Field(min_length=1)]
    reason: FabricationReason
    field: Annotated[str, Field(min_length=1)] | None


class FabricationCensus(StrictMigrationModel):
    """What a staged import wrote, and what it wrote without a source.

    Attributes:
        imported_rows: How many records the import writes.
        rows_by_entity_kind: How many of each epoch-2 kind, keyed by the
            kind's URN token, so a Task or Run count is readable without
            walking the plan.
        rows_without_source_locator: The address of every row whose
            disposition cannot cite the source row it read.
        findings: Every unsupported row and field, in import order.
    """

    imported_rows: Annotated[int, Field(ge=0)]
    rows_by_entity_kind: dict[str, int]
    rows_without_source_locator: tuple[str, ...]
    findings: tuple[FabricationFinding, ...]

    @classmethod
    def build(cls, staged: StagedImport) -> FabricationCensus:
        """Census what ``staged`` writes and what it cannot justify.

        Args:
            staged: The reduced import.

        Returns:
            The census, naming every row the source does not entail.
        """
        by_kind: dict[str, int] = {}
        missing: list[str] = []
        findings: list[FabricationFinding] = []
        for row in staged.rows:
            by_kind[row.entity_kind.value] = by_kind.get(row.entity_kind.value, 0) + 1
            if not row.has_source_locator:
                missing.append(row.address)
                findings.append(
                    FabricationFinding(
                        address=row.address,
                        reason=FabricationReason.NO_SOURCE_LOCATOR,
                        field=None,
                    )
                )
            findings.extend(
                FabricationFinding(address=row.address, reason=claim.reason, field=claim.field)
                for claim in row.unsupported_claims
            )
        return cls(
            imported_rows=len(staged.rows),
            rows_by_entity_kind=dict(sorted(by_kind.items())),
            rows_without_source_locator=tuple(missing),
            findings=tuple(findings),
        )

    def rows_of_kind(self, kind: EntityKind) -> int:
        """Return how many rows the import writes of one epoch-2 kind."""
        return self.rows_by_entity_kind.get(kind.value, 0)


class ImportValidationReport(StrictMigrationModel):
    """Whether a staged import is safe to seal a manifest over.

    Attributes:
        identity: The addressing slots the import was minted under.
        snapshot_digest: The corpus revision the report covers.
        aliases: How the epoch-1 identifiers resolve.
        references: How the references resolve.
        fabrication: What the import wrote without a source fact.
        mapping_rules: The rule versions the import was produced under.
        report_digest: A digest over every other field. Two runs over one
            snapshot produce the same digest, which is what lets the
            cutover plan run the validator twice and compare.
    """

    identity: CorpusIdentity
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    aliases: ImportAliasIndex
    references: ReferenceCensus
    fabrication: FabricationCensus
    mapping_rules: tuple[MappingRuleVersion, ...]
    report_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def build(
        cls,
        *,
        snapshot: SourceSnapshot,
        allowlist_path: Path,
        identity: CorpusIdentity,
    ) -> ImportValidationReport:
        """Import ``snapshot`` and validate what the import produced.

        The plan is built here rather than passed in, so the report can
        never describe a plan taken from a different corpus than the
        snapshot it digests.

        Args:
            snapshot: The frozen epoch-1 corpus.
            allowlist_path: Location of the shared allowed-legacy-symbol
                allowlist.
            identity: The addressing slots minted URNs are rendered
                under.

        Returns:
            The report. Reporting is this method's job; refusing is
            :meth:`require_clean`.

        Raises:
            FileNotFoundError: When ``allowlist_path`` does not exist.
            IdentityError: A minted key or URN violates the grammar.
            MigrationFabricationDetectedError: When a measurement
                re-points at a Task the import never wrote, or the
                document records no project code.
            MigrationSourceUnreadableError: When the snapshot holds no
                audit ledger.
            MigrationCountMismatchError: When a source row reaches no arm
                of a rule that must be total.
            ValidationError: When a source row is unreadable.
        """
        plan = CorpusImportPlan.build(snapshot=snapshot, allowlist_path=allowlist_path)
        staged = StagedImport.reduce(plan=plan, snapshot=snapshot)
        aliases = ImportAliasIndex.build(staged=staged, identity=identity)
        references = ReferenceCensus.build(staged)
        fabrication = FabricationCensus.build(staged)
        rules = tuple(mapping_rule_index(load_legacy_symbol_allowlist(allowlist_path)).values())
        payload = _report_payload(
            identity=identity,
            snapshot_digest=snapshot.identity.snapshot_digest,
            aliases=aliases.census,
            references=references,
            fabrication=fabrication,
            rules=rules,
        )
        return cls(
            identity=identity,
            snapshot_digest=snapshot.identity.snapshot_digest,
            aliases=aliases,
            references=references,
            fabrication=fabrication,
            mapping_rules=rules,
            report_digest=rule_digest(payload),
        )

    def digest_payload(self) -> bytes:
        """Return the exact bytes :attr:`report_digest` is taken over.

        Returns:
            The canonical JSON the digest covers, so a second run can be
            compared byte for byte rather than only by digest.
        """
        return canonical_json(
            _report_payload(
                identity=self.identity,
                snapshot_digest=self.snapshot_digest,
                aliases=self.aliases.census,
                references=self.references,
                fabrication=self.fabrication,
                rules=self.mapping_rules,
            )
        )

    def require_clean(self) -> None:
        """Refuse a staged import that must not be written.

        Raises:
            IdentityError: When the epoch-1 identifiers do not resolve
                once and injectively.
            MigrationFabricationDetectedError: When a canonical reference
                dangles, or a row the source does not entail was written.
                The message names every offender, because a defect an
                operator cannot locate is a defect they cannot fix.
        """
        self.aliases.resolvable_index()
        problems = [
            f"{row.source_address}.{row.field} -> {row.reference}"
            for row in self.references.dangling
        ]
        problems.extend(
            f"{finding.address} {finding.reason.value}" for finding in self.fabrication.findings
        )
        if problems:
            raise MigrationFabricationDetectedError(
                f"{len(problems)} rows of the staged import are not entailed by the source: "
                f"{', '.join(problems)}"
            )


def _report_payload(
    *,
    identity: CorpusIdentity,
    snapshot_digest: str,
    aliases: AliasCensus,
    references: ReferenceCensus,
    fabrication: FabricationCensus,
    rules: tuple[MappingRuleVersion, ...],
) -> dict[str, Any]:
    """Return the digestable form of one validation report."""
    return {
        "identity": identity.model_dump(mode="json"),
        "snapshot_digest": snapshot_digest,
        "aliases": aliases.model_dump(mode="json"),
        "references": references.model_dump(mode="json"),
        "fabrication": fabrication.model_dump(mode="json"),
        "mapping_rules": [rule.model_dump(mode="json") for rule in rules],
    }


def validation_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the validation tables."""
    return {
        "canonical_references": {
            field: kind.value for field, kind in CANONICAL_REFERENCE_KINDS.items()
        },
        "measurement_reference": TASK_REF_FIELD,
        "legacy_reference_populations": dict(LEGACY_REFERENCE_POPULATIONS),
        "lifecycle_entity_kinds": {
            target.value: kind.value for target, kind in LIFECYCLE_ENTITY_KINDS.items()
        },
        "run_source_kinds": [ATTEMPT_RUN_SOURCE_KIND, CLAIM_RUN_SOURCE_KIND],
        "fabrication_reasons": [reason.value for reason in FabricationReason],
    }
