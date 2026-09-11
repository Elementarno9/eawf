"""Read-only plan mode: what the cutover would do, before it does anything.

Seven steps, none of which writes. The snapshot is pinned, censused,
imported in memory, mapped onto the manifest tables, read for the
evidence and the open questions, and then validated twice with the two
reports compared byte for byte. What comes out is a sealed
:class:`~eawf.kernel.migration.epoch2.manifest.MigrationManifest` inside a
:class:`MigrationPlan` whose approval digest is the thing an operator
approves and a later apply is checked against.

Two runs over one pinned revision produce the same manifest digest. That
is not a convenience: the manifest digest is what ties an approval to a
corpus, so a digest that moved between two reads of the same bytes would
make every approval meaningless. The seal fields move -- they record when
and by whom -- which is exactly why the content digest excludes them.

The import is staged in memory here. Writing the staged records into
their tiers is a separate, durable step; plan mode exists so every
mapping, omission and open question can be read before any of that runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, model_validator

from eawf.kernel.migration.epoch2.allowlist import load_legacy_symbol_allowlist
from eawf.kernel.migration.epoch2.census import SourceCensus
from eawf.kernel.migration.epoch2.dispositions import COLLECTION_DISPOSITION_INDEX
from eawf.kernel.migration.epoch2.errors import (
    MigrationFabricationDetectedError,
    MigrationPlanNotApplicableError,
    MigrationSourceChangedError,
    MigrationValidationDivergedError,
)
from eawf.kernel.migration.epoch2.lifecycle import (
    MILESTONE_TRACK_FIELD,
    ImportedLifecycleRecord,
    LifecycleSourceIndex,
    track_deferral,
)
from eawf.kernel.migration.epoch2.manifest import (
    CONVERTING_DISPOSITIONS,
    GIT_EVIDENCE_FIELDS,
    MANIFEST_SCHEMA_VERSION,
    OUTCOME_TRACK_FIELD,
    UNADDRESSED_TARGETS,
    GitEvidence,
    GitFact,
    ManifestSource,
    MigrationManifest,
    OperatorAssignment,
    RollbackBoundary,
    RowMapping,
    SealState,
    StoreMapping,
    TargetCensus,
    UnresolvedReason,
    UnresolvedRow,
    ValidationPass,
    idempotence_payload,
    manifest_content_payload,
)
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.registry import mapping_rule_index
from eawf.kernel.migration.epoch2.rows import COLLECTION_ROW_CONTRACT_INDEX, SourceShape
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    StrictMigrationModel,
    rule_digest,
)
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.migration.epoch2.validation import (
    CorpusIdentity,
    ImportValidationReport,
    StagedImport,
)
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, Epoch2Collection, tier_for

logger = logging.getLogger(__name__)


#: The JSON-RPC method the cutover plan is served under. The name is part
#: of the contract rather than of the transport, so it lives beside the
#: request model both sides of the wire validate against.
EPOCH2_PLAN_METHOD = "migration.epoch2.plan"


class Epoch2PlanRequest(StrictMigrationModel):
    """One request for a read-only cutover plan.

    Epoch 1 recorded a project code and nothing about the workspace or
    repository the corpus lands in, so all three addressing slots are
    required here: a default would be a guess wearing a value's clothes.

    Attributes:
        snapshot_root: The staging directory holding the epoch-1 corpus.
        allowlist_path: Location of the shared allowed-legacy-symbol
            allowlist.
        workspace_key: The addressing workspace.
        project_key: The addressing project.
        repository_key: The addressing repository.
        sealed_by: The principal recorded in the manifest's seal.
    """

    snapshot_root: Annotated[str, Field(min_length=1)]
    allowlist_path: Annotated[str, Field(min_length=1)]
    workspace_key: Annotated[str, Field(min_length=1)]
    project_key: Annotated[str, Field(min_length=1)]
    repository_key: Annotated[str, Field(min_length=1)]
    sealed_by: Annotated[str, Field(min_length=1, max_length=64)]


class PlanStep(StrEnum):
    """The seven read-only steps plan mode runs, in order.

    The order is the dependency order, not a preference: nothing can be
    mapped before the source is censused, and the validator cannot be
    compared against itself before it has run twice.
    """

    READ_BARRIER = "read_barrier"
    SOURCE_CENSUS = "source_census"
    STAGE_IMPORT = "stage_import"
    MAP_ROWS = "map_rows"
    COLLECT_EVIDENCE = "collect_evidence"
    VALIDATE_FIRST = "validate_first_pass"
    VALIDATE_SECOND = "validate_second_pass"


#: The steps in the one order plan mode runs them.
PLAN_STEPS: tuple[PlanStep, ...] = tuple(PlanStep)


class PlanStepResult(StrictMigrationModel):
    """What one plan step found, as the plan reports it.

    Attributes:
        step: Which step ran.
        order: Its 1-based position in the sequence.
        summary: The counts the step produced, for an operator reading
            the plan rather than the manifest.
    """

    step: PlanStep
    order: Annotated[int, Field(ge=1, le=len(PLAN_STEPS))]
    summary: Annotated[str, Field(min_length=1, max_length=300)]


class MigrationPlan(StrictMigrationModel):
    """One read-only pass over a corpus, and the approval digest for it.

    Attributes:
        manifest: The sealed record of what the cutover would do.
        steps: The seven steps, in order.
        approval_digest: What an operator approves and a later apply is
            checked against. It covers the manifest's content and
            placement digests plus the step sequence, so approving a plan
            approves the work, not the moment it was computed.
    """

    manifest: MigrationManifest
    steps: tuple[PlanStepResult, ...]
    approval_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _steps_are_the_declared_sequence(self) -> Self:
        """Refuse a plan that skipped, reordered or repeated a step.

        Returns:
            The validated plan.

        Raises:
            ValueError: When the recorded steps are not the seven
                declared steps in declared order, or when the approval
                digest does not cover them.
        """
        recorded = tuple(row.step for row in self.steps)
        if recorded != PLAN_STEPS:
            raise ValueError(
                f"plan mode runs {len(PLAN_STEPS)} steps in a fixed order; this plan "
                f"records {[step.value for step in recorded]}"
            )
        orders = tuple(row.order for row in self.steps)
        if orders != tuple(range(1, len(PLAN_STEPS) + 1)):
            raise ValueError(f"the plan steps are numbered {orders}, not 1 through 7")
        expected = approval_digest_of(manifest=self.manifest, steps=self.steps)
        if self.approval_digest != expected:
            raise ValueError(
                f"approval digest {self.approval_digest} does not cover this plan "
                f"(expected {expected})"
            )
        return self

    def require_applicable(self) -> None:
        """Refuse a plan an apply must not run.

        A required operator assignment does not refuse: the record
        imports with the field unset and annotated, and an operator fills
        it afterwards. An unresolved row does refuse: there is no target
        to write it to, so applying would shrink the corpus silently.

        Raises:
            MigrationPlanNotApplicableError: When the plan names at least
                one unresolved row. The message names every one of them,
                because a row an operator cannot locate is a row they
                cannot resolve.
        """
        rows = self.manifest.unresolved_rows
        if not rows:
            return
        named = ", ".join(f"{row.address} ({row.reason.value})" for row in rows)
        raise MigrationPlanNotApplicableError(
            f"{len(rows)} source rows have no epoch-2 target, so the cutover cannot "
            f"account for them: {named}"
        )

    def require_source_unchanged(self, root: Path) -> None:
        """Refuse a plan whose corpus has moved since the digest was taken.

        Args:
            root: The snapshot root the plan was taken over.

        Raises:
            MigrationSourceChangedError: When the corpus at ``root`` no
                longer digests to the revision the plan names.
            MigrationSourceUnreadableError: When a declared surface is
                gone, which is itself a change the plan cannot survive.
            MigrationDuplicateKeyError: When the re-read document repeats
                a key.
        """
        current = SourceSnapshot.read(root).identity.snapshot_digest
        if current != self.manifest.source_digest:
            raise MigrationSourceChangedError(
                f"the plan was taken over source revision {self.manifest.source_digest} "
                f"but the corpus now digests to {current}"
            )

    def step(self, step: PlanStep) -> PlanStepResult:
        """Return the result of one step.

        Args:
            step: Which step to look up.

        Returns:
            Its result.

        Raises:
            KeyError: When the plan holds no such step, which the
                sequence validator makes unreachable.
        """
        for row in self.steps:
            if row.step is step:
                return row
        raise KeyError(step)


def approval_digest_of(*, manifest: MigrationManifest, steps: tuple[PlanStepResult, ...]) -> str:
    """Return the digest an operator approves one plan by.

    Args:
        manifest: The plan's sealed manifest.
        steps: The plan's step results, in order.

    Returns:
        A 64-character lowercase hex digest.
    """
    return rule_digest(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "source_digest": manifest.source_digest,
            "manifest_digest": manifest.manifest_digest,
            "idempotence_digest": manifest.idempotence_digest,
            "steps": [[row.order, row.step.value, row.summary] for row in steps],
        }
    )


def build_migration_plan(
    *,
    snapshot_root: Path,
    allowlist_path: Path,
    identity: CorpusIdentity,
    sealed_at: datetime,
    sealed_by: str,
) -> MigrationPlan:
    """Run the seven read-only steps over one corpus and seal the result.

    Nothing is written. ``sealed_at`` is a parameter rather than a clock
    read so two runs of this function over one revision differ only in
    the seal -- which is what the byte-identical manifest digest rests on.

    Args:
        snapshot_root: The staging directory holding the epoch-1 corpus.
        allowlist_path: Location of the shared allowed-legacy-symbol
            allowlist.
        identity: The workspace, project and repository slots minted URNs
            are addressed under.
        sealed_at: When the plan's seal is taken.
        sealed_by: The principal the seal records.

    Returns:
        The plan, with its manifest sealed and its approval digest taken.

    Raises:
        FileNotFoundError: When ``allowlist_path`` does not exist.
        MigrationSourceUnreadableError: When a declared read surface is
            missing or unparseable.
        MigrationDuplicateKeyError: When a decoded object repeats a key.
        MigrationCollectionOmittedError: When a declared collection is
            absent from the source.
        MigrationCollectionUnknownError: When the source carries an
            undeclared collection.
        MigrationRowValidationError: When any source row fails its
            declared schema. A corpus the importer cannot read yields no
            plan at all, rather than a plan over the readable half.
        MigrationCountMismatchError: When a source row reaches no arm of
            a rule that must be total.
        MigrationFabricationDetectedError: When the import would write a
            row the source does not entail.
        MigrationValidationDivergedError: When the two validation passes
            do not agree byte for byte.
        ValidationError: When any assembled model violates its contract.
    """
    steps: list[PlanStepResult] = []

    snapshot = SourceSnapshot.read(snapshot_root)
    _note(steps, PlanStep.READ_BARRIER, f"pinned {len(snapshot.identity.surfaces)} read surfaces")

    census = SourceCensus.build(snapshot)
    census.require_importable()
    _note(
        steps,
        PlanStep.SOURCE_CENSUS,
        f"censused {len(census.collections)} declared collections with "
        f"{len(census.proof_forms())} drop proofs",
    )

    corpus = CorpusImportPlan.build(snapshot=snapshot, allowlist_path=allowlist_path)
    staged = StagedImport.reduce(plan=corpus, snapshot=snapshot)
    _note(steps, PlanStep.STAGE_IMPORT, f"staged {len(staged.rows)} records in memory")

    placement = _Placement.of(snapshot=snapshot, census=census, corpus=corpus, staged=staged)
    _note(
        steps,
        PlanStep.MAP_ROWS,
        f"mapped {len(placement.row_mappings)} collections onto "
        f"{placement.target_census.total_rows} records in "
        f"{len(placement.store_mappings)} stores",
    )

    _note(
        steps,
        PlanStep.COLLECT_EVIDENCE,
        f"read {len(placement.git_evidence.facts)} git facts, "
        f"{len(placement.track_assignments)} required operator assignments and "
        f"{len(placement.unresolved_rows)} unresolved rows",
    )

    first = ImportValidationReport.build(
        snapshot=snapshot, allowlist_path=allowlist_path, identity=identity
    )
    first.require_clean()
    _note(steps, PlanStep.VALIDATE_FIRST, f"first pass clean at digest {first.report_digest[:12]}")

    second = ImportValidationReport.build(
        snapshot=snapshot, allowlist_path=allowlist_path, identity=identity
    )
    second.require_clean()
    _require_passes_identical(first, second)
    _note(
        steps,
        PlanStep.VALIDATE_SECOND,
        f"second pass byte-identical over {len(second.digest_payload())} bytes",
    )

    manifest = _assemble_manifest(
        snapshot=snapshot,
        census=census,
        identity=identity,
        placement=placement,
        rules=_rule_versions(allowlist_path=allowlist_path, corpus=corpus),
        passes=(
            ValidationPass.of(first, pass_index=1),
            ValidationPass.of(second, pass_index=2),
        ),
        source_schema_version=corpus.lifecycle.source_index.source_schema_version,
    ).sealed(sealed_at=sealed_at, sealed_by=sealed_by)

    frozen = tuple(steps)
    return MigrationPlan(
        manifest=manifest,
        steps=frozen,
        approval_digest=approval_digest_of(manifest=manifest, steps=frozen),
    )


def plan_cutover(request: Epoch2PlanRequest, *, sealed_at: datetime) -> MigrationPlan:
    """Run plan mode for one already-validated request.

    Both callers -- the daemon handler and the in-process CLI arm -- come
    through here, so the plan they produce cannot differ by which side of
    the wire computed it.

    Args:
        request: The validated request.
        sealed_at: When the plan's seal is taken.

    Returns:
        The sealed plan.

    Raises:
        MigrationRuleError: When an importer rule refuses the corpus.
        FileNotFoundError: When the allowlist does not exist.
        IdentityError: When a minted key or URN violates the grammar.
        ValidationError: When an assembled model violates its contract.
    """
    return build_migration_plan(
        snapshot_root=Path(request.snapshot_root),
        allowlist_path=Path(request.allowlist_path),
        identity=CorpusIdentity(
            workspace_key=request.workspace_key,
            project_key=request.project_key,
            repository_key=request.repository_key,
        ),
        sealed_at=sealed_at,
        sealed_by=request.sealed_by,
    )


def plan_envelope(plan: MigrationPlan) -> dict[str, Any]:
    """Return the envelope one plan is reported as, over the wire or locally.

    The summary fields are lifted out of the manifest so a caller can
    decide whether to apply without walking the whole document, and the
    whole plan rides along so nothing about the decision is lost.

    Args:
        plan: The sealed plan.

    Returns:
        The JSON-ready envelope.
    """
    manifest = plan.manifest
    return {
        "status": "ok",
        "source_digest": manifest.source_digest,
        "manifest_digest": manifest.manifest_digest,
        "idempotence_digest": manifest.idempotence_digest,
        "approval_digest": plan.approval_digest,
        "target_rows": manifest.target_census.total_rows,
        "unresolved_row_count": len(manifest.unresolved_rows),
        "required_operator_assignment_count": len(manifest.track_assignments),
        "applicable": not manifest.unresolved_rows,
        "steps": [
            {"order": row.order, "step": row.step.value, "summary": row.summary}
            for row in plan.steps
        ],
        "plan": plan.model_dump(mode="json"),
    }


class _Placement(StrictMigrationModel):
    """Where every source collection's rows land, and what is still open."""

    row_mappings: tuple[RowMapping, ...]
    store_mappings: tuple[StoreMapping, ...]
    target_census: TargetCensus
    git_evidence: GitEvidence
    track_assignments: tuple[OperatorAssignment, ...]
    unresolved_rows: tuple[UnresolvedRow, ...]

    @classmethod
    def of(
        cls,
        *,
        snapshot: SourceSnapshot,
        census: SourceCensus,
        corpus: CorpusImportPlan,
        staged: StagedImport,
    ) -> _Placement:
        """Derive the placement of one staged import over one snapshot.

        Args:
            snapshot: The frozen epoch-1 corpus.
            census: Its inventory, read for the per-collection row counts
                and drop proofs so the mapping and the census cannot
                disagree about what the source holds.
            corpus: The staged import.
            staged: That import reduced to its rows.

        Returns:
            The placement.

        Raises:
            MigrationFabricationDetectedError: When a staged row cites no
                source collection, so its rows cannot be counted against
                one.
            ValidationError: When an assembled model violates its
                contract.
        """
        document = snapshot.document
        index = corpus.lifecycle.source_index
        staged_ids = _staged_ids(staged)
        planned = _planned_rows(document)
        assignments = _track_assignments(corpus=corpus, document=document, index=index)
        unresolved = _unresolved_rows(document=document, staged_ids=staged_ids, planned=planned)
        mappings = _row_mappings(
            census=census,
            staged_counts=_staged_counts(staged),
            planned=planned,
            unresolved=unresolved,
            assignments=assignments,
        )
        by_collection = _collection_counts(staged=staged, planned=planned)
        return cls(
            row_mappings=mappings,
            store_mappings=_store_mappings(by_collection),
            target_census=TargetCensus.of(
                by_entity_kind=_kind_counts(staged),
                by_collection=by_collection,
                planned_rows=sum(planned.values()),
            ),
            git_evidence=GitEvidence.of(_git_facts(corpus)),
            track_assignments=assignments,
            unresolved_rows=unresolved,
        )


def _note(steps: list[PlanStepResult], step: PlanStep, summary: str) -> None:
    """Append one step result in the declared order.

    Args:
        steps: The accumulating step list, appended in place.
        step: Which step finished.
        summary: What it found.

    Raises:
        ValidationError: When the step is appended out of declared order.
    """
    order = PLAN_STEPS.index(step) + 1
    steps.append(PlanStepResult(step=step, order=order, summary=summary))


def _require_passes_identical(
    first: ImportValidationReport, second: ImportValidationReport
) -> None:
    """Refuse two validation passes that are not byte-identical.

    The digests are compared through the payload bytes rather than the
    digest strings, so a divergence is reported with the lengths an
    operator can act on instead of two opaque hashes.

    Args:
        first: The first pass.
        second: The second pass.

    Raises:
        MigrationValidationDivergedError: When the two payloads differ.
    """
    left, right = first.digest_payload(), second.digest_payload()
    if left == right:
        return
    raise MigrationValidationDivergedError(
        f"two validation passes over one pinned revision differ: {len(left)} bytes "
        f"digesting to {first.report_digest} then {len(right)} bytes digesting to "
        f"{second.report_digest}"
    )


def _source_collection_of(source_kind: str) -> str:
    """Return the top-level collection one staged row's source kind names.

    A Run is minted from an attempt entry inside a wave, so its source
    kind is ``waves.sessions``; the collection it counts against is still
    ``waves``.

    Args:
        source_kind: The staged row's source kind.

    Returns:
        The top-level collection name.
    """
    return source_kind.split(".", 1)[0]


def _require_source_locator(row_address: str, source_kind: str | None) -> str:
    """Return a staged row's source kind, refusing a row that cites none.

    Args:
        row_address: The row's address, for the message.
        source_kind: Its source kind, or ``None``.

    Returns:
        The source kind.

    Raises:
        MigrationFabricationDetectedError: When the row cites no source
            surface, so there is no collection to count it against.
    """
    if source_kind is None:
        raise MigrationFabricationDetectedError(
            f"{row_address} cites no source surface, so the manifest cannot count it "
            "against a source collection"
        )
    return source_kind


def _staged_counts(staged: StagedImport) -> dict[str, int]:
    """Count staged records per source collection.

    Args:
        staged: The reduced import.

    Returns:
        One count per collection the import wrote records from.

    Raises:
        MigrationFabricationDetectedError: When a staged row cites no
            source surface.
    """
    counts: dict[str, int] = {}
    for row in staged.rows:
        collection = _source_collection_of(_require_source_locator(row.address, row.source_kind))
        counts[collection] = counts.get(collection, 0) + 1
    return counts


def _staged_ids(staged: StagedImport) -> dict[str, frozenset[str]]:
    """Index the source row ids the import placed, per source collection.

    Args:
        staged: The reduced import.

    Returns:
        One id set per collection. A Run's id is its attempt address
        rather than a source row key, which is harmless here: the sets
        are only ever intersected with the document's own keys.

    Raises:
        MigrationFabricationDetectedError: When a staged row cites no
            source surface.
    """
    placed: dict[str, set[str]] = {}
    for row in staged.rows:
        collection = _source_collection_of(_require_source_locator(row.address, row.source_kind))
        if row.source_id is not None:
            placed.setdefault(collection, set()).add(row.source_id)
    return {collection: frozenset(ids) for collection, ids in placed.items()}


def _kind_counts(staged: StagedImport) -> dict[str, int]:
    """Count staged records per epoch-2 entity kind."""
    counts: dict[str, int] = {}
    for row in staged.rows:
        counts[row.entity_kind.value] = counts.get(row.entity_kind.value, 0) + 1
    return counts


def _keyed_rows(document: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    """Return one collection's object rows keyed by id, or nothing."""
    value = document.get(collection)
    if not isinstance(value, Mapping):
        return {}
    return {str(key): row for key, row in value.items() if key and isinstance(row, Mapping)}


def _planned_rows(document: Mapping[str, Any]) -> dict[str, int]:
    """Count the rows planned outside the identity traversal, per collection.

    Args:
        document: The decoded epoch-1 document.

    Returns:
        One count per collection in :data:`UNADDRESSED_TARGETS` that holds
        rows. A Track outcome has no key of its own in epoch 2, so its
        rows are planned here rather than minted.
    """
    counts: dict[str, int] = {}
    for collection in UNADDRESSED_TARGETS:
        rows = len(_keyed_rows(document, collection))
        if rows:
            counts[collection] = rows
    return counts


def _collection_counts(*, staged: StagedImport, planned: Mapping[str, int]) -> dict[str, int]:
    """Count every record the cutover writes, per epoch-2 collection.

    Args:
        staged: The reduced import.
        planned: Rows planned outside the identity traversal.

    Returns:
        One count per epoch-2 collection receiving records.
    """
    counts: dict[str, int] = {}
    for row in staged.rows:
        collection = ENTITY_COLLECTIONS[row.entity_kind].value
        counts[collection] = counts.get(collection, 0) + 1
    for source_collection, rows in planned.items():
        collection = UNADDRESSED_TARGETS[source_collection].value
        counts[collection] = counts.get(collection, 0) + rows
    return counts


def _store_mappings(by_collection: Mapping[str, int]) -> tuple[StoreMapping, ...]:
    """Route every receiving collection to its declared tier.

    Args:
        by_collection: Records per epoch-2 collection.

    Returns:
        One mapping per receiving collection, in collection-name order.

    Raises:
        ValidationError: When a routed tier is not the declared one.
        KeyError: When a collection name is not an epoch-2 collection.
    """
    rows: list[StoreMapping] = []
    for name, count in sorted(by_collection.items()):
        collection = Epoch2Collection(name)
        rows.append(StoreMapping(collection=collection, tier=tier_for(collection), row_count=count))
    return tuple(rows)


def _required_records(*, collection: str, row_count: int | None) -> int:
    """Return how many records one collection's source content must produce.

    A keyed-row collection owes one record per row. A non-row mapping --
    the project block, the workspace block -- is a single entity however
    many fields it holds, so counting its fields as rows would demand
    records the target has no place for.

    Args:
        collection: The epoch-1 top-level key.
        row_count: How many rows the census counted, or ``None``.

    Returns:
        The number of records to account for.
    """
    shape = COLLECTION_ROW_CONTRACT_INDEX[collection].shape
    if shape is SourceShape.MAPPING:
        return 1 if row_count else 0
    return row_count or 0


def _unresolved_rows(
    *,
    document: Mapping[str, Any],
    staged_ids: Mapping[str, frozenset[str]],
    planned: Mapping[str, int],
) -> tuple[UnresolvedRow, ...]:
    """Name every source row the cutover has no target for.

    Only a collection with a declared conversion can leave a row
    unresolved. An explicit drop, a derived projection and document
    metadata each write nothing by design, and a collection with a drop
    proof holds nothing to write.

    Args:
        document: The decoded epoch-1 document.
        staged_ids: The source row ids the import placed, per collection.
        planned: Rows planned outside the identity traversal.

    Returns:
        The unresolved rows, ordered by collection then by row id.

    Raises:
        ValidationError: When an assembled row violates its contract.
    """
    rows: list[UnresolvedRow] = []
    for collection in sorted(COLLECTION_DISPOSITION_INDEX):
        declared = COLLECTION_DISPOSITION_INDEX[collection]
        if declared.disposition not in CONVERTING_DISPOSITIONS:
            continue
        if collection in planned:
            continue
        placed = staged_ids.get(collection, frozenset())
        rows.extend(_unresolved_in(collection=collection, document=document, placed=placed))
    return tuple(rows)


def _unresolved_in(
    *, collection: str, document: Mapping[str, Any], placed: frozenset[str]
) -> Iterable[UnresolvedRow]:
    """Yield the unresolved rows of one converting collection.

    Args:
        collection: The epoch-1 top-level key.
        document: The decoded epoch-1 document.
        placed: The row ids the import placed for this collection.

    Returns:
        The unresolved rows, in row-id order.
    """
    detail = (
        f"{collection!r} declares a conversion no importer rule implements, so the "
        "cutover has nowhere to write the row"
    )
    shape = COLLECTION_ROW_CONTRACT_INDEX[collection].shape
    if shape is SourceShape.MAPPING:
        value = document.get(collection)
        if not isinstance(value, Mapping) or not value or placed:
            return ()
        return (
            UnresolvedRow(
                address=collection,
                source_collection=collection,
                reason=UnresolvedReason.NO_CONVERTER,
                detail=detail,
            ),
        )
    return tuple(
        UnresolvedRow(
            address=f"{collection}/{row_id}",
            source_collection=collection,
            reason=UnresolvedReason.NO_CONVERTER,
            detail=detail,
        )
        for row_id in sorted(_keyed_rows(document, collection))
        if row_id not in placed
    )


def _row_mappings(
    *,
    census: SourceCensus,
    staged_counts: Mapping[str, int],
    planned: Mapping[str, int],
    unresolved: tuple[UnresolvedRow, ...],
    assignments: tuple[OperatorAssignment, ...],
) -> tuple[RowMapping, ...]:
    """Build one row mapping per declared source collection.

    The row count and the drop proof come from the census row rather than
    from a second pass over the document, so the manifest's own check
    that the two agree cannot be satisfied by two copies of one bug.

    Args:
        census: The epoch-1 inventory.
        staged_counts: Staged records per source collection.
        planned: Rows planned outside the identity traversal.
        unresolved: Every row the cutover cannot place.
        assignments: Every required operator assignment.

    Returns:
        One mapping per declared collection, in collection-name order.

    Raises:
        ValidationError: When a mapping contradicts its declared tables.
    """
    unresolved_counts = _tally(row.source_collection for row in unresolved)
    assignment_counts = _tally(row.source_collection for row in assignments)
    rows: list[RowMapping] = []
    for collection in sorted(COLLECTION_DISPOSITION_INDEX):
        declared = COLLECTION_DISPOSITION_INDEX[collection]
        censused = census.collection(collection)
        writes = declared.disposition in CONVERTING_DISPOSITIONS and censused.proof_form is None
        rows.append(
            RowMapping(
                source_collection=collection,
                disposition=declared.disposition,
                target_collection=declared.target_collection,
                source_tier=declared.tier,
                shape=censused.shape,
                source_row_count=censused.row_count,
                target_row_count=(
                    staged_counts.get(collection, 0) + planned.get(collection, 0) if writes else 0
                ),
                unresolved_row_count=unresolved_counts.get(collection, 0),
                proof_form=censused.proof_form,
                operator_assignment_count=assignment_counts.get(collection, 0),
            )
        )
    _log_unplaced(rows)
    return tuple(rows)


def _tally(values: Iterable[str]) -> dict[str, int]:
    """Count how often each value appears."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _log_unplaced(rows: Iterable[RowMapping]) -> None:
    """Log the collections whose rows the cutover cannot place yet."""
    open_rows = [row for row in rows if row.unresolved_row_count]
    if open_rows:
        named = ", ".join(
            f"{row.source_collection}={row.unresolved_row_count}" for row in open_rows
        )
        logger.info(f"_log_unplaced unresolved={named}")


def _track_assignments(
    *,
    corpus: CorpusImportPlan,
    document: Mapping[str, Any],
    index: LifecycleSourceIndex,
) -> tuple[OperatorAssignment, ...]:
    """Collect every Track ownership the source cannot decide.

    Two populations ask the question. A Milestone asks which Track owns
    the delivery; the lifecycle mapper has already deferred the field and
    recorded why. A Track outcome metric asks which Track it hangs off;
    epoch 1 kept goals in a flat collection with no Track at all, so the
    question is open for every one of them.

    Args:
        corpus: The staged import, read for the lifecycle deferrals.
        document: The decoded epoch-1 document.
        index: The populations a Track reference resolves against.

    Returns:
        The assignments, lifecycle records first and then the planned
        outcome rows, each in source order.

    Raises:
        ValidationError: When an assembled assignment violates its
            contract.
    """
    rows = [
        _assignment_of(record=record, deferred_field=MILESTONE_TRACK_FIELD)
        for record in corpus.lifecycle.records
        if MILESTONE_TRACK_FIELD in record.deferred_field_names()
    ]
    for collection, target in UNADDRESSED_TARGETS.items():
        for row_id, row in sorted(_keyed_rows(document, collection).items()):
            deferrals = track_deferral(
                index.resolve_track(row.get("track_id")), target_field=OUTCOME_TRACK_FIELD
            )
            rows.extend(
                OperatorAssignment(
                    address=f"{collection}/{row_id}",
                    source_collection=collection,
                    target_collection=target.value,
                    target_field=deferral.target_field,
                    reason=deferral.reason,
                    candidates=deferral.candidates,
                )
                for deferral in deferrals
            )
    return tuple(rows)


def _assignment_of(*, record: ImportedLifecycleRecord, deferred_field: str) -> OperatorAssignment:
    """Build the assignment one lifecycle record's deferral asks for.

    Args:
        record: The imported record carrying the deferral.
        deferred_field: Which deferred field to read.

    Returns:
        The assignment.

    Raises:
        KeyError: When the record defers no such field.
        ValidationError: When the assembled assignment violates its
            contract.
    """
    deferral = next(
        field for field in record.deferred_fields if field.target_field == deferred_field
    )
    source_id = record.origin.source_id
    return OperatorAssignment(
        address=f"{record.source_collection}/{source_id}"
        if source_id
        else record.source_collection,
        source_collection=record.source_collection,
        target_collection=record.target.value,
        target_field=deferral.target_field,
        reason=deferral.reason,
        candidates=deferral.candidates,
    )


def _git_facts(corpus: CorpusImportPlan) -> tuple[GitFact, ...]:
    """Read every git reference the source recorded off the imported records.

    The references survive the import as legacy refs because epoch 2 has
    no native home for them, which makes the imported record the one
    place to read them from without re-deciding what counts as evidence.

    Args:
        corpus: The staged import.

    Returns:
        The facts, in record order and then in field order.

    Raises:
        ValidationError: When a recorded value is not a usable reference.
    """
    facts: list[GitFact] = []
    for record in corpus.lifecycle.records:
        source_id = record.origin.source_id or record.source_collection
        address = f"{record.source_collection}/{source_id}"
        for field, kind in sorted(GIT_EVIDENCE_FIELDS.items()):
            value = record.legacy_refs.get(field)
            if isinstance(value, str) and value:
                facts.append(GitFact(address=address, field=field, kind=kind, value=value))
    return tuple(facts)


def _rule_versions(
    *, allowlist_path: Path, corpus: CorpusImportPlan
) -> tuple[MappingRuleVersion, ...]:
    """Return every rule version the plan ran under, de-duplicated.

    Args:
        allowlist_path: Location of the shared allowed-legacy-symbol
            allowlist.
        corpus: The staged import, read for the rules it published.

    Returns:
        The rule versions in rule-id order.

    Raises:
        FileNotFoundError: When ``allowlist_path`` does not exist.
        ValueError: When the allowlist cannot be parsed.
    """
    allowlist = load_legacy_symbol_allowlist(allowlist_path)
    published = {rule.rule_id: rule for rule in mapping_rule_index(allowlist).values()}
    for rule in corpus.lifecycle.mapping_rules:
        published.setdefault(rule.rule_id, rule)
    return tuple(published[rule_id] for rule_id in sorted(published))


def _assemble_manifest(
    *,
    snapshot: SourceSnapshot,
    census: SourceCensus,
    identity: CorpusIdentity,
    placement: _Placement,
    rules: tuple[MappingRuleVersion, ...],
    passes: tuple[ValidationPass, ...],
    source_schema_version: str,
) -> MigrationManifest:
    """Assemble the draft manifest of one plan run.

    Args:
        snapshot: The frozen epoch-1 corpus.
        census: Its inventory.
        identity: The addressing slots minted URNs are rendered under.
        placement: Where every collection's rows land.
        rules: The rule versions the plan ran under.
        passes: The two validation passes.
        source_schema_version: The epoch-1 schema version the rows were
            read from.

    Returns:
        The manifest, in draft: plan mode has written nothing, so the
        rollback boundary is plan-only and there is no backup.

    Raises:
        ValidationError: When any manifest rule does not hold.
    """
    source = ManifestSource(
        snapshot=snapshot.identity,
        identity=identity,
        source_schema_version=source_schema_version,
    )
    content = manifest_content_payload(
        source=source,
        source_census=census,
        target_census=placement.target_census,
        mapping_rules=rules,
        row_mappings=placement.row_mappings,
        store_mappings=placement.store_mappings,
        git_evidence=placement.git_evidence,
        track_assignments=placement.track_assignments,
        unresolved_rows=placement.unresolved_rows,
        validation_results=passes,
    )
    return MigrationManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        source=source,
        source_census=census,
        target_census=placement.target_census,
        mapping_rules=rules,
        row_mappings=placement.row_mappings,
        store_mappings=placement.store_mappings,
        git_evidence=placement.git_evidence,
        track_assignments=placement.track_assignments,
        unresolved_rows=placement.unresolved_rows,
        source_digest=snapshot.identity.snapshot_digest,
        manifest_digest=rule_digest(content),
        idempotence_digest=rule_digest(
            idempotence_payload(
                target_census=placement.target_census,
                row_mappings=placement.row_mappings,
                store_mappings=placement.store_mappings,
            )
        ),
        backup=None,
        rollback_boundary=RollbackBoundary.PLAN_ONLY,
        validation_results=passes,
        seal_state=SealState.DRAFT,
        sealed_at=None,
        sealed_by=None,
        seal_digest=None,
    )


__all__ = [
    "EPOCH2_PLAN_METHOD",
    "PLAN_STEPS",
    "Epoch2PlanRequest",
    "MigrationPlan",
    "PlanStep",
    "PlanStepResult",
    "approval_digest_of",
    "build_migration_plan",
    "plan_cutover",
    "plan_envelope",
]
