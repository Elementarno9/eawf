"""The closed epoch-1 to epoch-2 status map and the annotated defaults.

A status is the one field a reader trusts without checking provenance,
so the map is declared here rather than left to importer discretion, and
it is closed: a source status outside it fails the plan instead of
importing under a guessed state.

Two Task fields the target requires from ``DRAFT`` are absent from most
epoch-1 rows. Their fills are annotations of this table, and the
annotation is data on the imported record rather than a note beside it,
so a later reader can tell a defaulted field from a recorded one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError

logger = logging.getLogger(__name__)


class SourceLifecycle(StrEnum):
    """The four epoch-1 collections that carry a lifecycle status."""

    PHASE = "phase"
    ITER = "iter"
    WAVE = "wave"
    BACKLOG = "backlog"


# ``archived`` maps to CANCELLED and never to COMPLETED: archival records
# that the phase stopped, and the source never recorded the acceptance
# COMPLETED would assert. ``abandoned`` reads the same way for an iter.
PHASE_STATUS_MAP: Mapping[str, str] = MappingProxyType(
    {
        "planned": "PLANNED",
        "active": "ACTIVE",
        "closed": "COMPLETED",
        "archived": "CANCELLED",
    }
)

ITER_STATUS_MAP: Mapping[str, str] = MappingProxyType(
    {
        "planned": "PLANNED",
        "active": "ACTIVE",
        "closed": "COMPLETED",
        "abandoned": "CANCELLED",
    }
)

# Every key is the source status verbatim, so the in-flight wave reads
# ``in_progress`` here — the spelling the epoch-1 corpus writes — even
# though the epoch-2 status it lands on is named RUNNING.
WAVE_STATUS_MAP: Mapping[str, str] = MappingProxyType(
    {
        "pending": "PLANNED",
        "claimed": "CLAIMED",
        "in_progress": "RUNNING",
        "closed": "COMPLETED",
        "abandoned": "CANCELLED",
        "failed": "FAILED",
    }
)

BACKLOG_STATUS_MAP: Mapping[str, str] = MappingProxyType(
    {
        "open": "DRAFT",
        "closed": "DROPPED",
    }
)

STATUS_MAPS: Mapping[SourceLifecycle, Mapping[str, str]] = MappingProxyType(
    {
        SourceLifecycle.PHASE: PHASE_STATUS_MAP,
        SourceLifecycle.ITER: ITER_STATUS_MAP,
        SourceLifecycle.WAVE: WAVE_STATUS_MAP,
        SourceLifecycle.BACKLOG: BACKLOG_STATUS_MAP,
    }
)


def map_source_status(lifecycle: SourceLifecycle, source_status: str) -> str:
    """Map one epoch-1 status onto its epoch-2 target status.

    Args:
        lifecycle: Which of the four source collections the row came from.
        source_status: The raw source status string.

    Returns:
        The epoch-2 status name.

    Raises:
        MigrationCountMismatchError: When ``source_status`` falls outside
            the closed map for ``lifecycle``. The map is total over the
            corpus by construction, so a miss means the source census and
            the target census can no longer reconcile.
    """
    table = STATUS_MAPS[lifecycle]
    mapped = table.get(source_status)
    if mapped is None:
        raise MigrationCountMismatchError(
            f"{lifecycle.value} status {source_status!r} is outside the closed status map "
            f"({', '.join(sorted(table))})"
        )
    return mapped


# Annotation names are persisted on the imported record, so they are part
# of the migration contract rather than internal labels.
PRIORITY_DEFAULT_ANNOTATION = "priority_default_p2"
INTENT_FROM_TITLE_ANNOTATION = "intent_from_title"

DEFAULT_PRIORITY = "P2"

# Compaction moves a terminal record from the document to its ledger.
# These two annotations travel with it: without them a later reader
# cannot tell a defaulted field from a recorded one without replaying
# the whole import.
COMPACTION_PRESERVED_ANNOTATIONS: frozenset[str] = frozenset(
    {PRIORITY_DEFAULT_ANNOTATION, INTENT_FROM_TITLE_ANNOTATION}
)


def apply_annotated_defaults(row: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fill the two defaulted Task fields and report the annotations earned.

    ``priority`` imports as ``P2`` when the source carries none, because
    epoch 1 never recorded a priority and ``P2`` is the neutral member of
    an ordering aid that never overrides a dependency edge. ``intent``
    imports from the source ``title`` when the source intent is null.

    Args:
        row: The source row. Read-only; a new dict is returned.

    Returns:
        A ``(record, annotations)`` pair. ``annotations`` is ordered
        deterministically so two runs over one row agree byte for byte.

    Raises:
        MigrationCountMismatchError: When ``intent`` must be defaulted
            but the source carries no usable ``title`` to take it from.
            Filling it by any other rule would be fabrication.
    """
    record = dict(row)
    annotations: list[str] = []

    if not record.get("priority"):
        record["priority"] = DEFAULT_PRIORITY
        annotations.append(PRIORITY_DEFAULT_ANNOTATION)

    if not record.get("intent"):
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            raise MigrationCountMismatchError(
                "intent defaults from the source title, but the row carries no title"
            )
        record["intent"] = title
        annotations.append(INTENT_FROM_TITLE_ANNOTATION)

    return record, tuple(annotations)


def compact_annotations(annotations: tuple[str, ...]) -> tuple[str, ...]:
    """Return the annotations that survive a record's move to its ledger.

    Args:
        annotations: The annotations carried on the in-document record.

    Returns:
        The subset preserved through compaction, in the input order.
    """
    return tuple(item for item in annotations if item in COMPACTION_PRESERVED_ANNOTATIONS)


def status_rule_payload() -> dict[str, Any]:
    """Return the digestable form of the status map and the defaults."""
    return {
        "status_maps": {lifecycle.value: dict(table) for lifecycle, table in STATUS_MAPS.items()},
        "annotated_defaults": {
            "priority": {"value": DEFAULT_PRIORITY, "annotation": PRIORITY_DEFAULT_ANNOTATION},
            "intent": {"source_field": "title", "annotation": INTENT_FROM_TITLE_ANNOTATION},
        },
        "compaction_preserved": sorted(COMPACTION_PRESERVED_ANNOTATIONS),
    }
