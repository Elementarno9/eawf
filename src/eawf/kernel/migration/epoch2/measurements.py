"""Re-pointing the estimate and actual collections at the imported Tasks.

Epoch 1 keyed both measurement collections by the scope they measure, not
by the measurement's own ``EST-`` / ``ACT-`` id. That map key is the only
field that names the subject, so it is what the re-point resolves against:
keying on the row id instead would compare a measurement identifier to a
Task identifier and resolve nothing.

Epoch 1 defaulted an actual's cost and token tally to zero, so a row no
runtime capture ever touched reads as a measured zero spend. Epoch 2 makes
every priced field nullable, and the import carries such a default zero as
null: a row that names neither the harness nor the model it ran on was
never captured, so its zero was the schema default, not a reading. A row
that does name its harness or model but still carries a zero cost was
never priced either: its tokens show usage nobody priced, or show nothing
was read at all. Its zero cost, and a zero token tally beside it, import
as null. Only a captured row with a recorded cost keeps its zeros.

Two fields travel verbatim. The quality marker says how good the number
is -- the confidence an estimate was made under, the state the measured
work stopped in -- and the calibration-exclusion flag says the row is an
honest record that must not calibrate anything. Re-deriving either from
the numbers would turn a disqualified measurement back into a reference
class, which is the specific way a calibration corpus goes wrong.

A measurement whose subject is not a Task -- a phase, a batch, a scope
that was never imported -- is not an error and not an orphan. It stays an
immutable legacy record: the number happened, and nothing in epoch 2
claims it measures a Task.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import (
    MigrationCountMismatchError,
    MigrationFabricationDetectedError,
)
from eawf.kernel.migration.epoch2.origins import build_legacy_origin
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.state.epoch2.values import EntityOrigin, OriginConfidence

logger = logging.getLogger(__name__)


class MeasurementKind(StrEnum):
    """The two measurement collections epoch 1 kept."""

    ESTIMATE = "estimate"
    ACTUAL = "actual"


#: The epoch-1 top-level key each measurement kind is read from.
MEASUREMENT_COLLECTIONS: Mapping[MeasurementKind, str] = MappingProxyType(
    {
        MeasurementKind.ESTIMATE: "estimates",
        MeasurementKind.ACTUAL: "actuals",
    }
)

#: The field that records how good each measurement is. The two kinds
#: mark quality differently and neither is derivable from the other, so
#: the table names both rather than picking one and coercing.
QUALITY_MARKER_FIELDS: Mapping[MeasurementKind, str] = MappingProxyType(
    {
        MeasurementKind.ESTIMATE: "confidence",
        MeasurementKind.ACTUAL: "status",
    }
)

#: The epoch-1 flag marking a measurement that must not calibrate.
CALIBRATION_EXCLUSION_FIELD = "calibration_excluded"

#: The epoch-2 field a re-pointed measurement writes its subject into.
TASK_REF_FIELD = "task_ref"

#: The source field carrying the measurement's own identifier. It is
#: preserved and never used as the subject key.
MEASUREMENT_ID_FIELD = "id"

#: The priced fields of an epoch-1 actual whose zero may be the schema
#: default rather than a reading.
PRICED_ACTUAL_FIELDS: tuple[str, ...] = ("actual_cost_usd", "actual_tokens")

#: The attribution fields an epoch-1 capture stamps. A row carrying neither
#: was never captured.
CAPTURE_ATTRIBUTION_FIELDS: tuple[str, ...] = ("harness", "model")

#: The priced field whose zero on a captured row means the capture never
#: priced the run.
COST_FIELD = "actual_cost_usd"


class ImportedMeasurement(StrictMigrationModel):
    """One epoch-1 measurement row as the importer will write it.

    Attributes:
        kind: Which measurement collection the row came from.
        source_collection: That collection's top-level key.
        map_key: The collection key, which is the subject the row
            measures.
        source_row_id: The row's own ``EST-`` / ``ACT-`` identifier,
            preserved and never resolved against.
        task_ref: The imported Task the row re-points at, or ``None``
            when the subject is not an imported Task.
        quality_marker: The quality field's value, verbatim, or ``None``
            when the source recorded none.
        calibration_excluded: The exclusion flag, verbatim, or ``None``
            when the source recorded none. Absent is not ``False``: one
            says nobody marked the row, the other says somebody marked it
            eligible.
        origin: The legacy origin the record carries.
        payload: The source row, preserved except that a default zero
            in an uncaptured actual's priced field is carried as null.
        nulled_fields: The priced fields carried as null for that reason,
            so the rewrite is stated on the record rather than inferred.
    """

    kind: MeasurementKind
    source_collection: Annotated[str, Field(min_length=1)]
    map_key: Annotated[str, Field(min_length=1)]
    source_row_id: Annotated[str, Field(min_length=1)] | None
    task_ref: Annotated[str, Field(min_length=1)] | None
    quality_marker: str | None
    calibration_excluded: bool | None
    origin: EntityOrigin
    payload: dict[str, Any]
    nulled_fields: tuple[str, ...] = ()

    @property
    def is_immutable_legacy_record(self) -> bool:
        """Whether the row imports as a legacy record rather than a re-point.

        Returns:
            ``True`` when no imported Task backs the row's subject, which
            is the case the source cannot re-point without inventing one.
        """
        return self.task_ref is None


def _quality_marker(*, kind: MeasurementKind, row: Mapping[str, Any]) -> str | None:
    """Return the row's quality marker verbatim, or ``None``."""
    value = row.get(QUALITY_MARKER_FIELDS[kind])
    if isinstance(value, str) and value:
        return value
    return None


def unpriced_default_fields(*, kind: MeasurementKind, row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the priced fields of *row* whose zero is the epoch-1 default.

    Args:
        kind: Which measurement collection the row came from.
        row: The source measurement row.

    Returns:
        The fields, in :data:`PRICED_ACTUAL_FIELDS` order; empty for an
        estimate or for a captured row whose cost is not zero.
    """
    if kind is not MeasurementKind.ACTUAL:
        return ()
    captured = any(row.get(name) is not None for name in CAPTURE_ATTRIBUTION_FIELDS)
    if captured and row.get(COST_FIELD) != 0:
        return ()
    return tuple(name for name in PRICED_ACTUAL_FIELDS if row.get(name) == 0)


def imported_payload(*, kind: MeasurementKind, row: Mapping[str, Any]) -> dict[str, Any]:
    """Return *row* as the import writes it, its default zeros carried as null.

    Args:
        kind: Which measurement collection the row came from.
        row: The source measurement row.

    Returns:
        A copy of the row.
    """
    nulled = unpriced_default_fields(kind=kind, row=row)
    return {name: None if name in nulled else value for name, value in row.items()}


def _calibration_excluded(row: Mapping[str, Any]) -> bool | None:
    """Return the row's exclusion flag verbatim, or ``None`` when unrecorded."""
    value = row.get(CALIBRATION_EXCLUSION_FIELD)
    if isinstance(value, bool):
        return value
    return None


def map_measurement_row(
    *,
    kind: MeasurementKind,
    map_key: str,
    row: Mapping[str, Any],
    task_ids: frozenset[str],
    source_schema_version: str,
) -> ImportedMeasurement:
    """Map one measurement row, re-pointing it only where a Task exists.

    Args:
        kind: Which measurement collection the row came from.
        map_key: The row's own key in that collection, which names the
            subject it measures.
        row: The source measurement row.
        task_ids: The source ids of every row imported as a Task.
        source_schema_version: The epoch-1 schema version, stamped on the
            origin.

    Returns:
        The imported measurement.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
        ValidationError: When a field violates the model contract.
    """
    task_ref = map_key if map_key in task_ids else None
    source_row_id = row.get(MEASUREMENT_ID_FIELD)
    confidence: OriginConfidence = "exact" if task_ref is not None else "supported"
    return ImportedMeasurement(
        kind=kind,
        source_collection=MEASUREMENT_COLLECTIONS[kind],
        map_key=map_key,
        source_row_id=source_row_id if isinstance(source_row_id, str) and source_row_id else None,
        task_ref=task_ref,
        quality_marker=_quality_marker(kind=kind, row=row),
        calibration_excluded=_calibration_excluded(row),
        origin=build_legacy_origin(
            source_kind=MEASUREMENT_COLLECTIONS[kind],
            source_id=map_key,
            row=row,
            source_schema_version=source_schema_version,
            confidence=confidence,
        ),
        payload=imported_payload(kind=kind, row=row),
        nulled_fields=unpriced_default_fields(kind=kind, row=row),
    )


class MeasurementImportPlan(StrictMigrationModel):
    """Both measurement collections as the importer will write them.

    Attributes:
        measurements: One record per source row, estimates before
            actuals and each in map-key order.
        orphan_keys: Every re-pointed row whose Task was not imported.
        legacy_keys: Every row whose subject is not an imported Task, so
            an operator can see what stayed a legacy record without
            walking the whole plan.
    """

    measurements: tuple[ImportedMeasurement, ...]
    orphan_keys: tuple[str, ...]
    legacy_keys: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        document: Mapping[str, Any],
        task_ids: frozenset[str],
        source_schema_version: str,
    ) -> MeasurementImportPlan:
        """Re-point every estimate and actual row of ``document``.

        Args:
            document: The decoded epoch-1 state document.
            task_ids: The source ids of every row imported as a Task.
            source_schema_version: The epoch-1 schema version.

        Returns:
            The import plan. Reporting orphans is this method's job;
            refusing them is :meth:`require_no_orphans`.

        Raises:
            TypeError: When a row holds a value ``json`` cannot encode.
            ValidationError: When a row violates the model contract.
        """
        rows: list[ImportedMeasurement] = []
        for kind in MeasurementKind:
            collection = document.get(MEASUREMENT_COLLECTIONS[kind])
            if not isinstance(collection, Mapping):
                continue
            for map_key in sorted(collection, key=str):
                row = collection[map_key]
                if not isinstance(row, Mapping):
                    continue
                rows.append(
                    map_measurement_row(
                        kind=kind,
                        map_key=str(map_key),
                        row=row,
                        task_ids=task_ids,
                        source_schema_version=source_schema_version,
                    )
                )
        return cls(
            measurements=tuple(rows),
            orphan_keys=tuple(
                row.map_key
                for row in rows
                if row.task_ref is not None and row.task_ref not in task_ids
            ),
            legacy_keys=tuple(row.map_key for row in rows if row.is_immutable_legacy_record),
        )

    def for_kind(self, kind: MeasurementKind) -> tuple[ImportedMeasurement, ...]:
        """Return every measurement imported from one collection.

        Args:
            kind: Which measurement collection to select.

        Returns:
            The records, in map-key order.
        """
        return tuple(row for row in self.measurements if row.kind is kind)

    def measurement_for(self, *, kind: MeasurementKind, map_key: str) -> ImportedMeasurement:
        """Return the measurement imported from one source row.

        Args:
            kind: Which measurement collection the row came from.
            map_key: The row's own key in that collection.

        Returns:
            Its imported record.

        Raises:
            KeyError: When no such row was imported.
        """
        for row in self.measurements:
            if row.kind is kind and row.map_key == map_key:
                return row
        raise KeyError(f"{MEASUREMENT_COLLECTIONS[kind]}.{map_key}")

    def require_no_orphans(self) -> None:
        """Refuse a plan whose re-points do not all land on imported Tasks.

        Raises:
            MigrationFabricationDetectedError: When any measurement
                re-points at a Task the import never wrote. Such a row
                would assert a subject the target does not hold.
        """
        if not self.orphan_keys:
            return
        keys = ", ".join(self.orphan_keys)
        raise MigrationFabricationDetectedError(
            f"{len(self.orphan_keys)} measurements re-point at Tasks that were never "
            f"imported: {keys}"
        )

    def require_reconciled(self, document: Mapping[str, Any]) -> None:
        """Refuse a plan that does not carry every source measurement verbatim.

        Each estimate and actual row of ``document`` must come out as
        exactly one imported record whose preserved row equals the source
        row, with only its default zeros carried as null
        (:func:`imported_payload`). Counting alone would pass a plan that
        dropped one row and duplicated another; comparing the rows is what
        keeps a null cost null, a measured zero a zero, and the quality
        marker and exclusion flag what the source recorded.

        Args:
            document: The decoded epoch-1 state document the plan was
                built from.

        Raises:
            MigrationCountMismatchError: When a collection's imported
                records do not match its source rows one for one.
            MigrationFabricationDetectedError: When an imported record's
                preserved row differs from its source row.
        """
        for kind in MeasurementKind:
            name = MEASUREMENT_COLLECTIONS[kind]
            collection = document.get(name)
            source: Mapping[str, Any] = collection if isinstance(collection, Mapping) else {}
            rows = self.for_kind(kind)
            missing = sorted(set(map(str, source)) - {row.map_key for row in rows})
            if missing or len(rows) != len(source):
                raise MigrationCountMismatchError(
                    f"{name} holds {len(source)} rows but the import carries {len(rows)}; "
                    f"not carried: {', '.join(missing) or '-'}"
                )
            rewritten = [
                row.map_key
                for row in rows
                if row.payload != imported_payload(kind=kind, row=source[row.map_key])
            ]
            if rewritten:
                raise MigrationFabricationDetectedError(
                    f"{len(rewritten)} {name} rows would import rewritten: {', '.join(rewritten)}"
                )


def measurement_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the measurement re-point table."""
    return {
        "collections": {kind.value: name for kind, name in MEASUREMENT_COLLECTIONS.items()},
        "keyed_on": "map_key",
        "never_keyed_on": MEASUREMENT_ID_FIELD,
        "quality_markers": {kind.value: field for kind, field in QUALITY_MARKER_FIELDS.items()},
        "exclusion_flag": CALIBRATION_EXCLUSION_FIELD,
        "target_reference": TASK_REF_FIELD,
        "unbacked_subject_imports_as": "immutable_legacy_record",
        "default_zero_imports_as_null": {
            "fields": list(PRICED_ACTUAL_FIELDS),
            "when_all_null": list(CAPTURE_ATTRIBUTION_FIELDS),
            "or_when_zero": COST_FIELD,
        },
    }
