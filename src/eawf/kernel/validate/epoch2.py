"""Validation of an epoch-2 generation document.

A generation document is keyed by collection, then by public key, and
each native row is one record of that collection's own model. The
epoch-1 :class:`~eawf.kernel.migration.v1_state.State` model knows none of
those collections, so it cannot judge the document; this module checks
every native row against its model and every Batch and Task against the
record it is filed under.

A row the epoch-2 cutover imported is the status, time and payload the
cutover writes around the epoch-1 record, so it is recognised by that
exact shape and checked against the import model of its collection
rather than against a native model. Every row, native or imported, must
be filed under the key it spells itself.

A record named by a reference may have left the document for its ledger
once it reached a terminal status, so a reference resolves against both
tiers. A document read outside its generation with no ledger beside it
cannot resolve such a reference, so there an unresolved reference is a
warning rather than a violation.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME
from eawf.kernel.migration.epoch2.cutover import (
    DOCUMENT_SCHEMA_KEY,
    LIFECYCLE_COLLECTIONS,
    ROW_PAYLOAD_FIELD,
    ROW_RECORDED_AT_FIELD,
    ROW_STATUS_FIELD,
)
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION
from eawf.kernel.migration.epoch2.native_records import ImportedNativeRecord
from eawf.kernel.spec.release import Release
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.campaign import Campaign
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.plan_revision import PlanRevision
from eawf.kernel.state.epoch2.question import OpenQuestion
from eawf.kernel.state.epoch2.repository import Repository
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.state.models import Project
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document
from eawf.kernel.store.ledger import latest_rows
from eawf.kernel.store.paths import ledger_dir, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The native model each document row of a collection must satisfy. A
#: collection absent here has no native document row model; its imported
#: rows are still checked.
NATIVE_MODELS: Final[Mapping[Epoch2Collection, type[BaseModel]]] = MappingProxyType(
    {
        Epoch2Collection.PROJECT: Project,
        Epoch2Collection.RUN: Run,
        Epoch2Collection.RELEASE: Release,
        Epoch2Collection.REPOSITORY: Repository,
        Epoch2Collection.TRACK: Track,
        Epoch2Collection.CAMPAIGN: Campaign,
        Epoch2Collection.MILESTONE: Milestone,
        Epoch2Collection.BATCH: DeliveryBatch,
        Epoch2Collection.TASK: Task,
        Epoch2Collection.PLAN_REVISION: PlanRevision,
        Epoch2Collection.PENDING_ACTION: PendingAction,
        Epoch2Collection.OPEN_QUESTION: OpenQuestion,
    }
)

#: The field a native row spells its own key in, where it is not ``key``.
_KEY_FIELDS: Final[Mapping[Epoch2Collection, str]] = MappingProxyType(
    {Epoch2Collection.PROJECT: "code", Epoch2Collection.PENDING_ACTION: "id"}
)

#: The top-level document keys that hold a value rather than a collection.
_DOCUMENT_SCALARS: Final = frozenset({DOCUMENT_SCHEMA_KEY, CANONICAL_SEQUENCE_KEY})

#: The exact fields of the row the cutover writes around an imported record.
_IMPORTED_ROW_FIELDS: Final = frozenset(
    {ROW_STATUS_FIELD, ROW_RECORDED_AT_FIELD, ROW_PAYLOAD_FIELD}
)

#: The collections an imported epoch-1 lifecycle row lands in; every other
#: imported row is a natively keyed record.
_LIFECYCLE_IMPORT_COLLECTIONS: Final = frozenset(LIFECYCLE_COLLECTIONS.values())


class _ImportedRow(BaseModel):
    """The status and time the cutover records beside an imported payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Annotated[str, Field(min_length=1)]
    recorded_at: datetime


class ImportedLifecyclePayload(BaseModel):
    """The field set of an imported epoch-1 lifecycle record.

    The import model itself belongs to the epoch-1 reader, which no live
    module may load, so the validator holds the same closed field set and
    types the legacy origin that marks the row as imported. Nested source
    material stays untyped: the cutover validated it on the way in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lifecycle: Annotated[str, Field(min_length=1)]
    source_collection: Annotated[str, Field(min_length=1)]
    target: Annotated[str, Field(min_length=1)]
    target_status: Annotated[str, Field(min_length=1)]
    origin: EntityOrigin
    record: dict[str, Any]
    legacy_refs: dict[str, Any]
    legacy_session_ref: str | None
    criteria: list[Any]
    minted_runs: list[Any]
    deferred_fields: list[Any]
    resolution: Any
    obsolete: bool
    annotations: list[str]
    ledger_annotations: list[str]


class _ImportedLifecycleRow(_ImportedRow):
    """An imported epoch-1 lifecycle row, filed under its source id."""

    payload: ImportedLifecyclePayload


class _ImportedNativeRow(_ImportedRow):
    """An imported natively keyed record, filed under its own record key."""

    payload: ImportedNativeRecord


@dataclass(frozen=True)
class RowViolation:
    """One generation-document row that fails its model or a reference.

    Attributes:
        code: The ``EPOCH2.<CATEGORY>.<SPECIFIC>`` code, part of the CLI
            contract.
        path: The row as ``<collection>/<key>``.
        message: What is wrong, naming no value the row holds.
    """

    code: str
    path: str
    message: str


@dataclass(frozen=True)
class GenerationReport:
    """Outcome of validating one generation document.

    Attributes:
        schema_errors: One line per top-level key that is not a collection
            or does not hold an object of rows.
        violations: One per row that fails its model, is filed under a
            key it does not spell, or names a record neither the document
            nor its ledger holds; the path is ``<collection>/<key>``.
        warnings: One per reference the document does not hold when no
            ledger is present to resolve it against, so it is unproven
            rather than missing.
    """

    schema_errors: list[str]
    violations: list[RowViolation]
    warnings: list[RowViolation]

    @property
    def ok(self) -> bool:
        """``True`` when there are no schema errors and no violations."""
        return not self.schema_errors and not self.violations


def is_generation_document(payload: Mapping[str, Any]) -> bool:
    """Return whether *payload* is an epoch-2 generation document.

    Args:
        payload: The decoded top-level JSON object.

    Returns:
        ``True`` when the payload carries the epoch-2 document schema
        version, which neither an epoch-1 state nor an envelope does.
    """
    return payload.get(DOCUMENT_SCHEMA_KEY) == MANIFEST_SCHEMA_VERSION


def _imported_key(collection: Epoch2Collection, row: Mapping[str, Any]) -> str:
    """Validate an imported *row* and return the key it spells.

    Raises:
        ValidationError: The row fails the import model of its collection.
    """
    if collection in _LIFECYCLE_IMPORT_COLLECTIONS:
        lifecycle = _ImportedLifecycleRow.model_validate(row)
        return str(lifecycle.payload.origin.source_id)
    return _ImportedNativeRow.model_validate(row).payload.record_key


def _model_errors(error: ValidationError) -> str:
    """Render *error* as ``<loc>: <msg>`` parts, leaving out the rejected values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<row>'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def _typed_rows(
    collections: Mapping[Epoch2Collection, Mapping[str, Any]],
) -> tuple[dict[Epoch2Collection, dict[str, BaseModel]], list[RowViolation]]:
    """Validate every imported row and every native row of a modelled collection.

    Returns:
        The native rows that validated under their own key, by collection
        and key, and one violation per row that did not.
    """
    typed: dict[Epoch2Collection, dict[str, BaseModel]] = {}
    violations: list[RowViolation] = []
    for collection in sorted(collections, key=lambda item: item.value):
        model = NATIVE_MODELS.get(collection)
        for key, row in collections[collection].items():
            path = f"{collection.value}/{key}"
            if not isinstance(row, dict):
                violations.append(
                    RowViolation(
                        code="EPOCH2.ROW.INVALID",
                        path=path,
                        message=f"holds a {type(row).__name__}, not an object",
                    )
                )
                continue
            imported = row.keys() == _IMPORTED_ROW_FIELDS
            record: BaseModel | None = None
            try:
                if imported:
                    own_key = _imported_key(collection, row)
                elif model is not None:
                    record = model.model_validate(row)
                    own_key = str(getattr(record, _KEY_FIELDS.get(collection, "key")))
                else:
                    continue
            except ValidationError as error:
                described = "imported row" if imported or model is None else model.__name__
                violations.append(
                    RowViolation(
                        code="EPOCH2.ROW.INVALID",
                        path=path,
                        message=f"not a valid {described}: {_model_errors(error)}",
                    )
                )
                continue
            if own_key != key:
                violations.append(
                    RowViolation(
                        code="EPOCH2.ROW.KEY_MISMATCH",
                        path=path,
                        message=f"is filed under {key!r} but spells its own key {own_key!r}",
                    )
                )
                continue
            if record is not None:
                typed.setdefault(collection, {})[key] = record
    return typed, violations


def _reference_violations(
    state_path: Path,
    collections: Mapping[Epoch2Collection, Mapping[str, Any]],
    typed: Mapping[Epoch2Collection, Mapping[str, BaseModel]],
) -> Iterator[RowViolation]:
    """Yield one finding per Batch or Task filed under a record no tier holds."""

    def held(target: Epoch2Collection) -> frozenset[str]:
        in_document = collections.get(target, {}).keys()
        return frozenset(in_document) | frozenset(latest_rows(ledger_path(state_path, target)))

    milestones = held(Epoch2Collection.MILESTONE)
    for key, row in typed.get(Epoch2Collection.BATCH, {}).items():
        batch = cast(DeliveryBatch, row)
        if batch.milestone_ref.entity_key not in milestones:
            yield RowViolation(
                code="EPOCH2.REF.MILESTONE_MISSING",
                path=f"{Epoch2Collection.BATCH.value}/{key}",
                message=(
                    f"names milestone {str(batch.milestone_ref)!r}, "
                    "which neither the document nor its ledger holds"
                ),
            )
    batches = held(Epoch2Collection.BATCH)
    for key, row in typed.get(Epoch2Collection.TASK, {}).items():
        task = cast(Task, row)
        if task.batch_ref is not None and task.batch_ref.entity_key not in batches:
            yield RowViolation(
                code="EPOCH2.REF.BATCH_MISSING",
                path=f"{Epoch2Collection.TASK.value}/{key}",
                message=(
                    f"names batch {str(task.batch_ref)!r}, "
                    "which neither the document nor its ledger holds"
                ),
            )


def validate_generation(state_path: Path) -> GenerationReport:
    """Validate the generation document at *state_path*.

    The document is read as every reader sees it, with its machine-local
    status projection laid over the committed file.

    Args:
        state_path: The generation's ``state.json``.

    Returns:
        The report. Row and reference checks run over the collections
        that hold an object even when another top-level key is malformed,
        so one run names every defect.

    Raises:
        FileNotFoundError: The document does not exist.
        ValueError: The document or its projection is not a JSON object.
        LedgerTornTailError: A ledger a reference is resolved against ends
            mid-line.
    """
    document = read_document(state_path)
    schema_errors: list[str] = []
    collections: dict[Epoch2Collection, Mapping[str, Any]] = {}
    for name, rows in document.items():
        if name in _DOCUMENT_SCALARS:
            continue
        try:
            collection = Epoch2Collection(name)
        except ValueError:
            schema_errors.append(f"{name}: not an epoch-2 collection")
            continue
        if not isinstance(rows, dict):
            schema_errors.append(f"{name}: holds a {type(rows).__name__}, not an object")
            continue
        collections[collection] = rows
    typed, violations = _typed_rows(collections)
    references = list(_reference_violations(state_path, collections, typed))
    # A generation's own directory is complete: a ledger it never wrote is
    # an empty ledger. A copy elsewhere with no ledger beside it is not.
    ledger_resolvable = (
        ledger_dir(state_path).is_dir() or state_path.parent.parent.name == GENERATIONS_DIRNAME
    )
    warnings: list[RowViolation] = []
    if ledger_resolvable:
        violations.extend(references)
    else:
        warnings.extend(
            RowViolation(
                code=row.code,
                path=row.path,
                message=f"{row.message}; no ledger sits beside the document to resolve it against",
            )
            for row in references
        )
    logger.debug(
        f"validate_generation schema_errors={len(schema_errors)} violations={len(violations)} "
        f"warnings={len(warnings)}"
    )
    return GenerationReport(schema_errors=schema_errors, violations=violations, warnings=warnings)
