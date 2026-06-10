"""Incident-timeline projection for the wave-detail chassis ``evidence`` tab.

An :class:`~eawf.kernel.state.models.Incident` carries only its metadata in
``state.json`` (severity / status / open + close stamps); the chronological
*timeline* of recorded events lives in the
:class:`~eawf.kernel.store.kinds.incident.IncidentPayload` JSONL records under
``<state_dir>/store/incident.jsonl``. An ``open`` and a ``close`` each append a
record keyed by the incident id (``<id>`` and ``<id>-CLOSE``), and every record
carries one or more :class:`~eawf.kernel.store.kinds.incident.TimelineEntry`
rows. This module loads those records, gathers every entry that belongs to the
selected incident, and renders them chronologically into the reused detail-card
``(label, value)`` row shape.

Honest absence is first-class, never a fabricated entry: an incident whose
store has no recorded timeline entry renders the exact :data:`NO_EVENTS_LINE`
honest-empty line rather than an empty section or a synthesised row. Every
figure is a pure function of the loaded entries so the projection is
unit-testable without mounting Textual; the detail modal stays a thin view over
it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.incident import IncidentPayload
from eawf.kernel.store.paths import store_path

logger = logging.getLogger(__name__)

#: The honest-absence line the timeline renders when an incident has no
#: recorded event -- the store carries no :class:`TimelineEntry` for the
#: incident, so there is nothing to chronicle. Surfacing this line (rather
#: than an empty section or a fabricated entry) keeps the "surface now, data
#: later" contract honest.
NO_EVENTS_LINE: str = "no timeline events recorded"


@dataclass(frozen=True)
class TimelineEvent:
    """One chronological incident timeline event: a stamp plus its prose.

    Attributes:
        at: The event's recorded timestamp.
        entry: The event's prose line (e.g. ``"opened: <title>"``).
    """

    at: datetime
    entry: str


def _record_ids_for_incident(incident_id: str) -> frozenset[str]:
    """Return the store record ids that hold *incident_id*'s timeline.

    The open mutator writes a record keyed by the bare incident id and the
    close mutator writes one keyed ``<incident_id>-CLOSE`` (see
    :mod:`eawf.workflow.evidence.incident`), so both ids carry timeline
    entries for one incident.

    Args:
        incident_id: The incident whose store records to enumerate.

    Returns:
        The frozenset of envelope ids whose payloads belong to *incident_id*.
    """
    return frozenset({incident_id, f"{incident_id}-CLOSE"})


def load_incident_timeline(state_path: Path, incident_id: str) -> tuple[TimelineEvent, ...]:
    """Load *incident_id*'s chronological timeline from the local store.

    Reads the ``incident.jsonl`` store, validates each record's
    :class:`IncidentPayload`, gathers every
    :class:`~eawf.kernel.store.kinds.incident.TimelineEntry` whose owning
    envelope id belongs to *incident_id*, and returns the entries sorted by
    their recorded timestamp so the detail tab reads oldest-first. A missing
    store file (or any malformed line) yields ``()`` so the caller folds the
    honest absence rather than crashing the drill-in seam -- an incident with
    no recorded event is a real state, not an error.

    Args:
        state_path: The state path the ``store/`` directory is resolved from.
        incident_id: The incident whose timeline to load.

    Returns:
        The incident's timeline events, sorted oldest-first; ``()`` when the
        store has no record or no entry for the incident.
    """
    path = store_path(state_path, StoreKind.INCIDENT)
    if not path.is_file():
        return ()
    record_ids = _record_ids_for_incident(incident_id)
    events: list[TimelineEvent] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(raw_line)
        except ValueError as exc:
            logger.debug(f"load_incident_timeline skip-line incident={incident_id!r} cause={exc!r}")
            continue
        if envelope.kind is not StoreKind.INCIDENT or envelope.id not in record_ids:
            continue
        payload = IncidentPayload.model_validate(envelope.payload)
        events.extend(TimelineEvent(at=row.at, entry=row.entry) for row in payload.timeline)
    events.sort(key=lambda event: event.at)
    return tuple(events)


def incident_timeline_rows(events: tuple[TimelineEvent, ...]) -> tuple[tuple[str, str], ...]:
    """Build the ``evidence`` tab ``(label, value)`` rows for a timeline.

    Each chronological event contributes one ``event`` row whose value pairs
    the recorded ISO timestamp with the event prose. An empty *events* yields
    a single honest :data:`NO_EVENTS_LINE` row rather than a fabricated entry,
    so an incident the store never chronicled reads as honestly empty.

    Args:
        events: The incident's timeline events, already sorted oldest-first
            (by :func:`load_incident_timeline`).

    Returns:
        Ordered ``(label, value)`` rows: one ``event`` row per timeline entry,
        or a single ``timeline`` row carrying :data:`NO_EVENTS_LINE` when the
        incident has no recorded event.
    """
    if not events:
        return (("timeline", NO_EVENTS_LINE),)
    return tuple(("event", f"{event.at.isoformat()}  {event.entry}") for event in events)


__all__ = [
    "NO_EVENTS_LINE",
    "TimelineEvent",
    "incident_timeline_rows",
    "load_incident_timeline",
]
