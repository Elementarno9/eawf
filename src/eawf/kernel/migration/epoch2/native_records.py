"""Native conversion for the epoch-1 rows epoch 2 keeps under their own key.

Decisions, incidents, sandbox policies and the project block each have an
epoch-2 collection of the same meaning, so none of them is an immutable
legacy envelope: the row lands in its native collection, keyed by the id
it already had. That key is the point. Rules, waivers and accepted-risk
criteria cite a Decision by its ``D-*`` id and by the ``urn:eawf:v1``
URN built from it; re-keying the record under a minted ordinal would
leave every one of those citations naming nothing.

None of these collections is addressed by the epoch-2 identity grammar --
a Decision, an Incident and a SandboxPolicy have no entity kind, and a
Project's key is the supplied project slot rather than a minted one -- so
their records are planned beside the identity traversal rather than
minted through it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import MigrationFabricationDetectedError
from eawf.kernel.migration.epoch2.origins import build_legacy_origin, source_digest
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


class NativeRecordCollection(StrEnum):
    """The epoch-1 collections that convert into a natively keyed record."""

    DECISIONS = "decisions"
    INCIDENTS = "incidents"
    SANDBOX_POLICIES = "sandbox_policies"
    PROJECT = "project"


#: Which epoch-2 collection each source collection's records land in.
NATIVE_RECORD_TARGETS: Mapping[NativeRecordCollection, Epoch2Collection] = MappingProxyType(
    {
        NativeRecordCollection.DECISIONS: Epoch2Collection.DECISION,
        NativeRecordCollection.INCIDENTS: Epoch2Collection.INCIDENT,
        NativeRecordCollection.SANDBOX_POLICIES: Epoch2Collection.SANDBOX_POLICY,
        NativeRecordCollection.PROJECT: Epoch2Collection.PROJECT,
    }
)

#: The epoch-1 URN kind each collection's records were cited under. An
#: incident, a sandbox policy and the project block were never URN-cited,
#: so they carry none rather than one invented at import.
NATIVE_URN_KINDS: Mapping[NativeRecordCollection, str] = MappingProxyType(
    {NativeRecordCollection.DECISIONS: "decision"}
)

#: The project block field that names the project; it keys the Project
#: record and owns every URN a converted record carries.
PROJECT_CODE_FIELD = "code"


class ImportedNativeRecord(StrictMigrationModel):
    """One epoch-1 row converted into its native epoch-2 collection.

    Attributes:
        source_collection: The epoch-1 top-level key the row came from.
        record_key: The row's own key, preserved; the project block is
            keyed by its project code.
        target: The epoch-2 collection the record lands in.
        urn: The ``urn:eawf:v1`` URN the source cited the row by, or
            ``None`` for a collection that was never URN-cited.
        origin: The legacy origin the record carries.
        payload: The source row, preserved.
        payload_digest: The digest of that row in ``sha256:<hex>`` form.
    """

    source_collection: NativeRecordCollection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    target: Epoch2Collection
    urn: Annotated[str, Field(min_length=1)] | None
    origin: EntityOrigin
    payload: dict[str, Any]
    payload_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def map_native_record(
    *,
    collection: NativeRecordCollection,
    record_key: str,
    row: Mapping[str, Any],
    project_code: str,
    source_schema_version: str,
) -> ImportedNativeRecord:
    """Convert one epoch-1 row into its native epoch-2 record.

    Args:
        collection: Which native collection the row came from.
        record_key: The key the record keeps.
        row: The source row, preserved whole.
        project_code: The project that owns the row's URN.
        source_schema_version: The epoch-1 schema version, stamped on the
            origin.

    Returns:
        The record, under its source key and carrying its source URN.

    Raises:
        ValueError: When the key cannot be spelled as a URN id.
        TypeError: When the row holds a value ``json`` cannot encode.
        ValidationError: When a field violates the model contract.
    """
    urn_kind = NATIVE_URN_KINDS.get(collection)
    return ImportedNativeRecord(
        source_collection=collection,
        record_key=record_key,
        target=NATIVE_RECORD_TARGETS[collection],
        urn=None if urn_kind is None else build_urn(urn_kind, owner=project_code, id=record_key),
        origin=build_legacy_origin(
            source_kind=collection.value,
            source_id=record_key,
            row=row,
            source_schema_version=source_schema_version,
            confidence="exact",
        ),
        payload=dict(row),
        payload_digest=source_digest(row),
    )


def recorded_project_code(document: Mapping[str, Any]) -> str:
    """Return the project code the epoch-1 document records.

    Args:
        document: The decoded epoch-1 document.

    Returns:
        The recorded code.

    Raises:
        MigrationFabricationDetectedError: When the document records none.
            The code keys the Project record and owns every Decision URN,
            so a guessed one would re-address every citation.
    """
    project = document.get(NativeRecordCollection.PROJECT.value)
    code = project.get(PROJECT_CODE_FIELD) if isinstance(project, Mapping) else None
    if not isinstance(code, str) or not code:
        raise MigrationFabricationDetectedError(
            "the epoch-1 document records no project code to key the Project record "
            "and its Decision URNs with"
        )
    return code


def _keyed_rows(document: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    """Return one collection's object rows keyed by id, or nothing."""
    value = document.get(collection)
    if not isinstance(value, Mapping):
        return {}
    return {str(key): row for key, row in value.items() if key and isinstance(row, Mapping)}


class NativeRecordImportPlan(StrictMigrationModel):
    """Every epoch-1 row that converts into a natively keyed record.

    Attributes:
        records: The converted records, collection by collection and each
            in source-key order.
    """

    records: tuple[ImportedNativeRecord, ...]

    @classmethod
    def build(
        cls, *, document: Mapping[str, Any], source_schema_version: str
    ) -> NativeRecordImportPlan:
        """Convert every native-record row of ``document``.

        Args:
            document: The decoded epoch-1 document.
            source_schema_version: The epoch-1 schema version.

        Returns:
            The import plan. A project block that holds nothing converts
            to nothing, which the census already records as a drop.

        Raises:
            MigrationFabricationDetectedError: When the document records no
                project code.
            ValueError: When a key cannot be spelled as a URN id.
            ValidationError: When a record violates its contract.
        """
        project = document.get(NativeRecordCollection.PROJECT.value)
        if not isinstance(project, Mapping) or not project:
            return cls(records=())
        code = recorded_project_code(document)
        records: list[ImportedNativeRecord] = []
        for collection in NativeRecordCollection:
            if collection is NativeRecordCollection.PROJECT:
                rows: Mapping[str, Mapping[str, Any]] = {code: project}
            else:
                rows = _keyed_rows(document, collection.value)
            records.extend(
                map_native_record(
                    collection=collection,
                    record_key=key,
                    row=rows[key],
                    project_code=code,
                    source_schema_version=source_schema_version,
                )
                for key in sorted(rows)
            )
        return cls(records=tuple(records))


def native_record_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the native-record tables."""
    return {
        "targets": {
            collection.value: target.value for collection, target in NATIVE_RECORD_TARGETS.items()
        },
        "urn_kinds": {collection.value: kind for collection, kind in NATIVE_URN_KINDS.items()},
        "project_key_field": PROJECT_CODE_FIELD,
        "record_key": "source_key",
    }


__all__ = [
    "NATIVE_RECORD_TARGETS",
    "NATIVE_URN_KINDS",
    "PROJECT_CODE_FIELD",
    "ImportedNativeRecord",
    "NativeRecordCollection",
    "NativeRecordImportPlan",
    "map_native_record",
    "native_record_rule_payload",
    "recorded_project_code",
]
