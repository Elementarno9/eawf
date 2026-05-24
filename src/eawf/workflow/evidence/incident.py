"""Incident-area mutators: open / close / view.

* ``open`` registers a new incident with a severity + title; status starts at
  ``open`` and the timeline begins with the open event.
* ``close`` records root cause + corrective action and *requires* ``--audit``
  of a complete audit per the audit-evidence guard.
* ``view`` returns the incident plus its timeline.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.surfaces.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity, IncidentStatus, StoreKind
from eawf.kernel.state.models import Incident, State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.guards import require_complete_audit

logger = logging.getLogger(__name__)


def open_incident(
    state: State,
    *,
    incident_id: str,
    scope_id: str,
    severity: IncidentSeverity,
    title: str,
) -> tuple[Envelope, Envelope]:
    """Open a new incident in place; return (record, event) envelopes."""
    incidents: dict[str, Incident] = dict(state.incidents or {})
    if incident_id in incidents:
        raise UserError(f"incident {incident_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    incident = Incident(
        id=incident_id,
        scope_id=scope_id,
        severity=severity,
        title=title,
        status=IncidentStatus.OPEN,
        opened_at=now,
        closed_at=None,
        root_cause=None,
        corrective_action_ids=[],
        report_artifact_id=None,
    )
    incidents[incident_id] = incident
    state.incidents = incidents
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=incident_id,
        kind=StoreKind.INCIDENT,
        scope_id=scope_id,
        summary=f"incident {incident_id} opened severity={severity.value}",
        payload={
            "severity": severity.value,
            "timeline": [
                {"at": now.isoformat(), "entry": f"opened: {title}"},
            ],
            "cause": IncidentCause.UNKNOWN.value,
            "corrective_action_ids": [],
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-incident-open-{incident_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="incident.open",
        actor="cli",
        command="incident open",
        args={
            "incident_id": incident_id,
            "severity": severity.value,
            "title": title,
        },
        summary=f"incident {incident_id} opened",
    )
    return record, event


def close_incident(
    state: State,
    *,
    incident_id: str,
    root_cause: str,
    corrective_action_ids: list[str],
    audit_id: str,
) -> tuple[Envelope, Envelope]:
    """Close an incident in place; requires complete audit."""
    incidents: dict[str, Incident] = dict(state.incidents or {})
    if incident_id not in incidents:
        raise UserError(f"incident {incident_id!r} not found", kind="NotFound")
    if incidents[incident_id].status == IncidentStatus.RESOLVED:
        raise UserError(f"incident {incident_id!r} already resolved", kind="InvalidInput")

    require_complete_audit(state, audit_id)

    now = datetime.now(UTC)
    prior = incidents[incident_id]
    updated = prior.model_copy(
        update={
            "status": IncidentStatus.RESOLVED,
            "root_cause": root_cause,
            "corrective_action_ids": list(corrective_action_ids),
            "closed_at": now,
        }
    )
    incidents[incident_id] = updated
    state.incidents = incidents
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=f"{incident_id}-CLOSE",
        kind=StoreKind.INCIDENT,
        scope_id=updated.scope_id,
        summary=f"incident {incident_id} closed",
        payload={
            "severity": updated.severity.value,
            "timeline": [
                {"at": now.isoformat(), "entry": f"closed: {root_cause}"},
            ],
            # The operator-supplied prose stays on Incident.root_cause and in
            # the timeline entry above; the typed payload cause is left
            # unclassified until a --cause CLI surface lands.
            "cause": IncidentCause.UNKNOWN.value,
            "corrective_action_ids": list(corrective_action_ids),
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-incident-close-{incident_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="incident.close",
        actor="cli",
        command="incident close",
        args={
            "incident_id": incident_id,
            "audit_id": audit_id,
            "root_cause": root_cause,
        },
        summary=f"incident {incident_id} closed",
    )
    return record, event


def view_incident(state: State, incident_id: str) -> Incident:
    """Read-only lookup."""
    incidents = state.incidents or {}
    if incident_id not in incidents:
        raise UserError(f"incident {incident_id!r} not found", kind="NotFound")
    return incidents[incident_id]
