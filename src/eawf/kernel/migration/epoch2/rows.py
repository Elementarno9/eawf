"""The strict schema every epoch-1 row is read through.

The disposition table decides what happens to a collection; this table
decides what a row of that collection has to look like before anything
happens to it at all. Both are total over the epoch-1 document, and for
the same reason: a row nobody declared a shape for is a row the importer
would have to guess about.

The contracts are deliberately narrow. Each names the fields that were
present and non-empty on every row of the corpus at the pinned revision,
plus the field that has to agree with the row's own key. Fields the
corpus carries inconsistently are not declared, because a contract that
reds on real source data is a contract that gets skipped.

A row that violates its contract produces a located failure rather than
an exception, so one pass reports every offending row instead of only the
first. Turning that report into a refusal is the caller's job.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


class SourceShape(StrEnum):
    """The three shapes a top-level epoch-1 key can hold.

    ``KEYED_ROWS`` is an object whose values are entity rows.
    ``MAPPING`` is an object that is not a row collection -- a pointer
    block or a settings blob -- so its values are read but never
    validated as rows. ``SCALAR`` is document metadata that carries no
    container at all.
    """

    KEYED_ROWS = "keyed_rows"
    MAPPING = "mapping"
    SCALAR = "scalar"


class RowFailureCode(StrEnum):
    """The ways a source row can fail the schema declared for it."""

    COLLECTION_NULL_FORBIDDEN = "collection_null_forbidden"
    COLLECTION_NOT_MAPPING = "collection_not_mapping"
    COLLECTION_NOT_SCALAR = "collection_not_scalar"
    ROW_KEY_EMPTY = "row_key_empty"
    ROW_NOT_OBJECT = "row_not_object"
    REQUIRED_FIELD_MISSING = "required_field_missing"
    REQUIRED_FIELD_NOT_STRING = "required_field_not_string"
    KEY_FIELD_MISMATCH = "key_field_mismatch"


class RowValidationFailure(StrictMigrationModel):
    """One source row that cannot be imported as it stands.

    Attributes:
        locator: Where the failure is, as ``collection``,
            ``collection.key`` or ``collection.key.field``. The locator
            is the whole point of the failure: an operator has to be able
            to open the source and find the row.
        collection: The top-level source key the failure belongs to.
        code: The stable failure code.
        detail: A one-line explanation of what the row holds instead.
    """

    locator: Annotated[str, Field(min_length=1, max_length=256)]
    collection: Annotated[str, Field(min_length=1, max_length=64)]
    code: RowFailureCode
    detail: Annotated[str, Field(min_length=1, max_length=300)]


class CollectionRowContract(StrictMigrationModel):
    """The schema one epoch-1 top-level key is read through.

    Attributes:
        source_collection: The top-level key.
        shape: What the key holds.
        nullable: Whether the key is allowed to serialize as JSON
            ``null``. Only the keys that actually did so at the pinned
            revision are nullable; anywhere else a null is a lost
            container rather than an empty one.
        key_field: The row field that must equal the row's own key, or
            ``None`` when the collection keys rows by something other
            than a field of the row.
        required_string_fields: Fields that must be present and hold a
            non-empty string.
    """

    source_collection: Annotated[str, Field(min_length=1, max_length=64)]
    shape: SourceShape
    nullable: bool
    key_field: Annotated[str, Field(min_length=1)] | None
    required_string_fields: tuple[Annotated[str, Field(min_length=1)], ...]


def _rows(
    source_collection: str,
    key_field: str | None,
    required_string_fields: tuple[str, ...],
    *,
    nullable: bool = False,
) -> CollectionRowContract:
    """Build one keyed-row contract (positional to keep the table readable)."""
    return CollectionRowContract(
        source_collection=source_collection,
        shape=SourceShape.KEYED_ROWS,
        nullable=nullable,
        key_field=key_field,
        required_string_fields=required_string_fields,
    )


def _mapping(source_collection: str, *, nullable: bool = False) -> CollectionRowContract:
    """Build one non-row mapping contract."""
    return CollectionRowContract(
        source_collection=source_collection,
        shape=SourceShape.MAPPING,
        nullable=nullable,
        key_field=None,
        required_string_fields=(),
    )


def _scalar(source_collection: str) -> CollectionRowContract:
    """Build one document-metadata scalar contract."""
    return CollectionRowContract(
        source_collection=source_collection,
        shape=SourceShape.SCALAR,
        nullable=False,
        key_field=None,
        required_string_fields=(),
    )


COLLECTION_ROW_CONTRACTS: tuple[CollectionRowContract, ...] = (
    _rows("phases", "id", ("id", "scope_id", "status", "title", "opened_at")),
    _rows("iters", "id", ("id", "phase_id", "status", "title", "opened_at")),
    _rows("waves", "id", ("id", "iter_id", "status", "title", "opened_at")),
    _rows("backlog", "id", ("id", "scope_id", "status", "title", "created_at")),
    _rows("agent_sessions", "id", ("id", "role", "runtime", "scope_id", "status", "started_at")),
    _rows(
        "worktrees",
        "id",
        ("id", "wave_id", "branch", "base_branch", "path", "status", "created_at"),
    ),
    # ``estimates`` and ``actuals`` key by the scope they measure, not by
    # their own row id, so the id field is real but is not the key.
    _rows(
        "estimates",
        "scope_id",
        ("id", "scope_id", "display", "confidence", "reference_class", "updated_at"),
    ),
    _rows("actuals", "scope_id", ("id", "scope_id", "status", "updated_at")),
    _rows("audits", "id", ("id", "scope_id", "kind", "status", "created_at")),
    _rows("decisions", "id", ("id", "scope_id", "status", "title", "rationale", "created_at")),
    _rows(
        "incidents",
        "id",
        ("id", "scope_id", "status", "title", "severity", "cause", "opened_at"),
    ),
    _rows("artifacts", "id", ("id", "kind", "uri", "urn", "created_at")),
    _rows("memory_index", "id", ("id", "scope_id", "status", "tier", "summary", "confidence")),
    _rows("goals", "id", ("id", "scope_id", "status", "title", "summary", "created_at")),
    _rows("sandbox_policies", "id", ("id", "scope_id", "scope_kind", "granted_at")),
    _rows("close_attempts", "id", ("id", "wave_id", "status", "outcome", "requested_at")),
    _rows("wave_integrations", "id", ("id", "wave_id", "kind", "status", "created_at")),
    # Keyed by a composite the row does not carry as a single field.
    _rows("wave_dependency_bindings", None, ("wave_id", "dep_wave_id", "bound_at")),
    _rows("wave_dependency_barriers", None, ()),
    _rows("plugins", "id", ("id",)),
    # The ten keys that serialized as JSON null at the pinned revision.
    # Their row shape is unmeasured, so each declares only the identity
    # field: a contract invented from nothing is worse than a narrow one.
    _rows("claims", "id", ("id",), nullable=True),
    _rows("hypotheses", "id", ("id",), nullable=True),
    _rows("open_questions", "id", ("id",), nullable=True),
    _rows("outcomes", "id", ("id",), nullable=True),
    _rows("tracks", "id", ("id",), nullable=True),
    _rows("mcp_servers", "id", ("id",), nullable=True),
    _rows("mcp_grants", "id", ("id",), nullable=True),
    _mapping("workspace", nullable=True),
    _mapping("health", nullable=True),
    _mapping("fleet_run", nullable=True),
    _mapping("current"),
    _mapping("project"),
    _mapping("indexes"),
    _scalar("dispatch_paused"),
    _scalar("schema_version"),
    _scalar("scope_kind"),
    _scalar("updated_at"),
    _scalar("urn"),
)


COLLECTION_ROW_CONTRACT_INDEX: dict[str, CollectionRowContract] = {
    contract.source_collection: contract for contract in COLLECTION_ROW_CONTRACTS
}


LEDGER_ROW_ID_FIELD = "id"


def _failure(
    *, locator: str, collection: str, code: RowFailureCode, detail: str
) -> RowValidationFailure:
    """Build one located failure."""
    return RowValidationFailure(locator=locator, collection=collection, code=code, detail=detail)


def _validate_row(
    *, contract: CollectionRowContract, key: str, row: Any
) -> list[RowValidationFailure]:
    """Validate one row of a keyed-row collection against ``contract``."""
    collection = contract.source_collection
    locator = f"{collection}.{key}"
    if not key:
        return [
            _failure(
                locator=f"{collection}.<empty>",
                collection=collection,
                code=RowFailureCode.ROW_KEY_EMPTY,
                detail="a row is keyed by the empty string",
            )
        ]
    if not isinstance(row, dict):
        return [
            _failure(
                locator=locator,
                collection=collection,
                code=RowFailureCode.ROW_NOT_OBJECT,
                detail=f"row holds {type(row).__name__}, expected a JSON object",
            )
        ]

    failures: list[RowValidationFailure] = []
    for field in contract.required_string_fields:
        if field not in row:
            failures.append(
                _failure(
                    locator=f"{locator}.{field}",
                    collection=collection,
                    code=RowFailureCode.REQUIRED_FIELD_MISSING,
                    detail=f"required field {field!r} is absent",
                )
            )
            continue
        value = row[field]
        if not isinstance(value, str) or not value:
            failures.append(
                _failure(
                    locator=f"{locator}.{field}",
                    collection=collection,
                    code=RowFailureCode.REQUIRED_FIELD_NOT_STRING,
                    detail=f"required field {field!r} holds {type(value).__name__}, "
                    f"expected a non-empty string",
                )
            )
    if contract.key_field is not None:
        keyed = row.get(contract.key_field)
        if isinstance(keyed, str) and keyed and keyed != key:
            failures.append(
                _failure(
                    locator=f"{locator}.{contract.key_field}",
                    collection=collection,
                    code=RowFailureCode.KEY_FIELD_MISMATCH,
                    detail=f"row is keyed {key!r} but carries {keyed!r}",
                )
            )
    return failures


def validate_collection(*, collection: str, value: Any) -> tuple[RowValidationFailure, ...]:
    """Validate one top-level source key against its declared contract.

    Args:
        collection: The top-level key, which must be declared.
        value: The raw JSON value the source held under that key.

    Returns:
        Every failure found, in source order. An importable collection
        returns an empty tuple.

    Raises:
        KeyError: When ``collection`` has no declared contract. The
            caller reconciles the collection set against the disposition
            table first, so reaching here undeclared is a programming
            error rather than a source defect.
    """
    contract = COLLECTION_ROW_CONTRACT_INDEX[collection]

    if value is None:
        if contract.nullable:
            return ()
        return (
            _failure(
                locator=collection,
                collection=collection,
                code=RowFailureCode.COLLECTION_NULL_FORBIDDEN,
                detail="collection serialized as null but is not a nullable slot",
            ),
        )

    if contract.shape is SourceShape.SCALAR:
        if isinstance(value, (dict, list, tuple, set)):
            return (
                _failure(
                    locator=collection,
                    collection=collection,
                    code=RowFailureCode.COLLECTION_NOT_SCALAR,
                    detail=f"metadata key holds {type(value).__name__}, expected a scalar",
                ),
            )
        return ()

    if not isinstance(value, dict):
        return (
            _failure(
                locator=collection,
                collection=collection,
                code=RowFailureCode.COLLECTION_NOT_MAPPING,
                detail=f"collection holds {type(value).__name__}, expected a JSON object",
            ),
        )

    if contract.shape is SourceShape.MAPPING:
        return tuple(
            _failure(
                locator=f"{collection}.<empty>",
                collection=collection,
                code=RowFailureCode.ROW_KEY_EMPTY,
                detail="a mapping entry is keyed by the empty string",
            )
            for key in value
            if not key
        )

    failures: list[RowValidationFailure] = []
    for key, row in value.items():
        failures.extend(_validate_row(contract=contract, key=key, row=row))
    return tuple(failures)


def validate_ledger_rows(
    *, ledger: str, rows: Iterable[Mapping[str, Any]]
) -> tuple[RowValidationFailure, ...]:
    """Validate the rows of one store ledger.

    A ledger row is addressed by its position in the file because it has
    no key of its own until the ``id`` field is known to be usable.

    Args:
        ledger: The ledger file stem, such as ``audit``.
        rows: The decoded ledger rows, in file order.

    Returns:
        Every failure found, in file order.
    """
    failures: list[RowValidationFailure] = []
    for index, row in enumerate(rows):
        locator = f"store/{ledger}.jsonl#{index}"
        if LEDGER_ROW_ID_FIELD not in row:
            failures.append(
                _failure(
                    locator=locator,
                    collection=ledger,
                    code=RowFailureCode.REQUIRED_FIELD_MISSING,
                    detail=f"required field {LEDGER_ROW_ID_FIELD!r} is absent",
                )
            )
            continue
        value = row[LEDGER_ROW_ID_FIELD]
        if not isinstance(value, str) or not value:
            failures.append(
                _failure(
                    locator=locator,
                    collection=ledger,
                    code=RowFailureCode.REQUIRED_FIELD_NOT_STRING,
                    detail=f"required field {LEDGER_ROW_ID_FIELD!r} holds "
                    f"{type(value).__name__}, expected a non-empty string",
                )
            )
    return tuple(failures)


def row_contract_payload() -> dict[str, Any]:
    """Return the digestable form of the row-contract table."""
    return {
        "collections": [contract.model_dump(mode="json") for contract in COLLECTION_ROW_CONTRACTS],
        "failure_codes": sorted(item.value for item in RowFailureCode),
        "shapes": sorted(item.value for item in SourceShape),
        "ledger_row_id_field": LEDGER_ROW_ID_FIELD,
    }
