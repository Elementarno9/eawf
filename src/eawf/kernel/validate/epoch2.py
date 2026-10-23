"""Validation of an epoch-2 generation document.

A generation document is keyed by collection, then by public key, and
each native row is one record of that collection's own model. The
epoch-1 :class:`~eawf.kernel.migration.v1_state.State` model knows none of
those collections, so it cannot judge the document; this module checks
every native row against its model and every Batch and Task against the
record it is filed under.

A row the epoch-2 cutover imported carries the epoch-1 record wrapped in
a payload instead of a native record. The cutover validated it on the way
in and no native model describes it, so it is counted as held but not
re-validated here.

A record named by a reference may have left the document for its ledger
once it reached a terminal status, so a reference resolves against both
tiers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from eawf.kernel.migration.epoch2.cutover import DOCUMENT_SCHEMA_KEY, ROW_PAYLOAD_FIELD
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.campaign import Campaign
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.state.epoch2.plan_revision import PlanRevision
from eawf.kernel.state.epoch2.question import OpenQuestion
from eawf.kernel.state.epoch2.repository import Repository
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.store.compaction import CANONICAL_SEQUENCE_KEY, read_document
from eawf.kernel.store.ledger import latest_rows
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The native model each document row of a collection must satisfy.
NATIVE_MODELS: Final[Mapping[Epoch2Collection, type[BaseModel]]] = MappingProxyType(
    {
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

#: The top-level document keys that hold a value rather than a collection.
_DOCUMENT_SCALARS: Final = frozenset({DOCUMENT_SCHEMA_KEY, CANONICAL_SEQUENCE_KEY})


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
        violations: One per native row that fails its model or names a
            record neither the document nor its ledger holds; the path is
            ``<collection>/<key>``.
    """

    schema_errors: list[str]
    violations: list[RowViolation]

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


def _is_imported(row: Mapping[str, Any]) -> bool:
    """Return whether *row* is the wrapper the cutover writes for an imported record."""
    return "urn" not in row and ROW_PAYLOAD_FIELD in row


def _model_errors(error: ValidationError) -> str:
    """Render *error* as ``<loc>: <msg>`` parts, leaving out the rejected values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '<row>'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def _typed_rows(
    collections: Mapping[Epoch2Collection, Mapping[str, Any]],
) -> tuple[dict[Epoch2Collection, dict[str, BaseModel]], list[RowViolation]]:
    """Validate every native row of a modelled collection.

    Returns:
        The rows that validated, by collection and key, and one violation
        per row that did not.
    """
    typed: dict[Epoch2Collection, dict[str, BaseModel]] = {}
    violations: list[RowViolation] = []
    for collection, model in NATIVE_MODELS.items():
        for key, row in collections.get(collection, {}).items():
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
            if _is_imported(row):
                continue
            try:
                typed.setdefault(collection, {})[key] = model.model_validate(row)
            except ValidationError as error:
                violations.append(
                    RowViolation(
                        code="EPOCH2.ROW.INVALID",
                        path=path,
                        message=f"not a valid {model.__name__}: {_model_errors(error)}",
                    )
                )
    return typed, violations


def _reference_violations(
    state_path: Path,
    collections: Mapping[Epoch2Collection, Mapping[str, Any]],
    typed: Mapping[Epoch2Collection, Mapping[str, BaseModel]],
) -> Iterator[RowViolation]:
    """Yield one violation per Batch or Task filed under a record no tier holds."""

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
                message=f"names milestone {str(batch.milestone_ref)!r}, which no tier holds",
            )
    batches = held(Epoch2Collection.BATCH)
    for key, row in typed.get(Epoch2Collection.TASK, {}).items():
        task = cast(Task, row)
        if task.batch_ref is not None and task.batch_ref.entity_key not in batches:
            yield RowViolation(
                code="EPOCH2.REF.BATCH_MISSING",
                path=f"{Epoch2Collection.TASK.value}/{key}",
                message=f"names batch {str(task.batch_ref)!r}, which no tier holds",
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
    violations.extend(_reference_violations(state_path, collections, typed))
    logger.debug(
        f"validate_generation schema_errors={len(schema_errors)} violations={len(violations)}"
    )
    return GenerationReport(schema_errors=schema_errors, violations=violations)
