"""The criteria disposition row: epoch-1 CriterionSpec and GateSpec to one criterion.

An epoch-1 criterion and the gates that score it become a single epoch-2
criterion. Four fields convert natively; every other field of either
model is carried on ``legacy_refs`` rather than discarded, because a
field the importer cannot map natively is still a fact the source
recorded. The field tables below are checked against the live models at
import time, so adding a field to either epoch-1 model fails here rather
than silently dropping it at cutover.

An imported criterion has no source atoms to cite -- epoch 1 never had
them -- so it takes ``policy_ref`` instead: it is required by the import
policy, not by a requirement atom.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field

from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.spec.common import CriterionSpec, GateSpec

logger = logging.getLogger(__name__)

LegacyImportPolicyRef = Literal["legacy-import"]

LEGACY_IMPORT_POLICY_REF: LegacyImportPolicyRef = "legacy-import"

DANGLING_GATE_ANNOTATION = "dangling_gate_ids_carried_as_legacy"
REVERSE_BOUND_GATE_ANNOTATION = "gate_bound_by_criterion_id"


class FieldDisposition(StrEnum):
    """How one epoch-1 field reaches the imported criterion."""

    NATIVE_CONVERSION = "native_conversion"
    LEGACY_REF = "legacy_ref"


class FieldRoute(StrictMigrationModel):
    """Where one epoch-1 field lands on the imported criterion."""

    disposition: FieldDisposition
    target_key: Annotated[str, Field(min_length=1)]


def _native(target_key: str) -> FieldRoute:
    """Route a field to a native epoch-2 field named ``target_key``."""
    return FieldRoute(disposition=FieldDisposition.NATIVE_CONVERSION, target_key=target_key)


def _legacy(target_key: str) -> FieldRoute:
    """Route a field to ``legacy_refs[target_key]``."""
    return FieldRoute(disposition=FieldDisposition.LEGACY_REF, target_key=target_key)


CRITERION_FIELD_ROUTES: Mapping[str, FieldRoute] = {
    "id": _native("id"),
    "text": _native("text"),
    "gate_ids": _native("gate_ids"),
    "waiver_reason": _native("waiver_reason"),
    "kind": _legacy("criterion_kind"),
    "acceptance_style": _legacy("acceptance_style"),
    "evidence_kind": _legacy("evidence_kind"),
    "measurable_signal": _legacy("measurable_signal"),
    "required": _legacy("criterion_required"),
    "quality_dimension": _legacy("quality_dimension"),
    "response": _legacy("response"),
    "oracle_tier": _legacy("oracle_tier"),
}

GATE_FIELD_ROUTES: Mapping[str, FieldRoute] = {
    "id": _native("gate_ids"),
    "criterion_id": _legacy("gate_criterion_ids"),
    "args": _legacy("gate_args"),
    "cadence": _legacy("gate_cadences"),
    "kind": _legacy("gate_kinds"),
    "policy": _legacy("gate_policies"),
    "required": _legacy("gate_required"),
    "timeout_s": _legacy("gate_timeouts"),
}


def _assert_total(
    model_fields: Iterable[str], routes: Mapping[str, FieldRoute], label: str
) -> None:
    """Fail import unless ``routes`` covers ``model_fields`` exactly.

    Raises:
        ValueError: When a model field has no route (it would be dropped)
            or a route names a field the model does not have (it would
            carry a fact nobody recorded).
    """
    declared = set(model_fields)
    routed = set(routes)
    if declared != routed:
        missing = sorted(declared - routed)
        extra = sorted(routed - declared)
        raise ValueError(f"{label} field routes are not total: unrouted={missing}, unknown={extra}")


_assert_total(CriterionSpec.model_fields, CRITERION_FIELD_ROUTES, "CriterionSpec")
_assert_total(GateSpec.model_fields, GATE_FIELD_ROUTES, "GateSpec")


class ImportedCriterion(StrictMigrationModel):
    """One epoch-2 criterion produced from epoch-1 criterion plus gates."""

    id: Annotated[str, Field(min_length=1)]
    text: Annotated[str, Field(min_length=1)]
    policy_ref: LegacyImportPolicyRef
    source_atom_refs: tuple[str, ...]
    gate_ids: tuple[str, ...]
    waiver_reason: str | None
    legacy_refs: dict[str, Any]
    annotations: tuple[str, ...]


def convert_criterion(
    *,
    criterion: CriterionSpec,
    gates: Iterable[GateSpec],
) -> ImportedCriterion:
    """Convert one epoch-1 criterion and its gates into an epoch-2 criterion.

    A gate id the criterion names but no co-resident gate carries is
    dangling: it is carried under ``legacy_refs.dangling_gate_ids``
    rather than emitted as a native reference to a gate that does not
    exist. A gate whose ``criterion_id`` binds to this criterion is
    included even when the criterion's own ``gate_ids`` omits it, so the
    reverse edge is not lost.

    Args:
        criterion: The epoch-1 criterion row.
        gates: Every co-resident epoch-1 gate row. Gates bound to another
            criterion are ignored.

    Returns:
        The imported criterion, carrying ``policy_ref`` and every
        unmapped epoch-1 field under ``legacy_refs``.
    """
    criterion_json = criterion.model_dump(mode="json")
    bound = [gate for gate in gates if gate.criterion_id == criterion.id]
    bound_ids = [gate.id for gate in bound]

    resolved = [gate_id for gate_id in criterion.gate_ids if gate_id in set(bound_ids)]
    dangling = [gate_id for gate_id in criterion.gate_ids if gate_id not in set(bound_ids)]
    reverse_only = [gate_id for gate_id in bound_ids if gate_id not in set(criterion.gate_ids)]

    legacy_refs: dict[str, Any] = {}
    for field, route in CRITERION_FIELD_ROUTES.items():
        if route.disposition is FieldDisposition.LEGACY_REF:
            legacy_refs[route.target_key] = criterion_json[field]

    for field, route in GATE_FIELD_ROUTES.items():
        if route.disposition is not FieldDisposition.LEGACY_REF:
            continue
        legacy_refs[route.target_key] = {
            gate.id: gate.model_dump(mode="json")[field] for gate in bound
        }

    annotations: list[str] = []
    if dangling:
        legacy_refs["dangling_gate_ids"] = list(dangling)
        annotations.append(DANGLING_GATE_ANNOTATION)
    if reverse_only:
        annotations.append(REVERSE_BOUND_GATE_ANNOTATION)

    return ImportedCriterion(
        id=criterion.id,
        text=criterion.text,
        policy_ref=LEGACY_IMPORT_POLICY_REF,
        source_atom_refs=(),
        gate_ids=tuple(resolved + reverse_only),
        waiver_reason=criterion.waiver_reason,
        legacy_refs=legacy_refs,
        annotations=tuple(annotations),
    )


def criteria_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the criteria field routes."""
    return {
        "criterion": {
            field: route.model_dump(mode="json") for field, route in CRITERION_FIELD_ROUTES.items()
        },
        "gate": {
            field: route.model_dump(mode="json") for field, route in GATE_FIELD_ROUTES.items()
        },
        "policy_ref": LEGACY_IMPORT_POLICY_REF,
    }
