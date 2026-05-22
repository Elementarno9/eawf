"""Telemetry aggregator — roll source rows into typed telemetry rows.

The aggregator is the pure, typed seam between the source adapters
(:mod:`eawf.telemetry.sources`) and the projector
(:mod:`eawf.telemetry.projector`). It owns two responsibilities (C09
§5.9.4):

* **Session rolling** — a :class:`~eawf.telemetry.models.TelemetrySession`
  yielded by a per-runtime adapter is stamped with the project it belongs
  to (the adapters leave ``project_id`` empty), producing the row the
  projector upserts.
* **Incident classification** — an :class:`~eawf.store.envelope.Envelope`
  carrying an incident-bearing payload is folded into a
  :class:`~eawf.telemetry.models.TelemetryIncident`. The incident *cause*
  is resolved through typed :class:`~eawf.state.enums.IncidentCause`
  lookups keyed on the closed event-type / runtime-error-class
  enumerations — **never** by substring-matching free prose. A payload
  that already carries a typed ``cause`` (an ``incident``-kind envelope)
  is adopted verbatim; an event-kind envelope's ``event_type`` /
  typed ``cause`` is mapped through the lookup tables below.

Every function here is pure: given the same envelope it returns the same
row, with no I/O and no hidden state. Malformed records the source
adapters already skipped never reach this layer; the aggregator
fail-fasts (raises) only on a structurally impossible input the caller
constructed wrong.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from eawf.state.enums import IncidentCause, IncidentSeverity, StoreKind
from eawf.store.envelope import Envelope
from eawf.telemetry.models import TelemetryIncident, TelemetrySession
from eawf.telemetry.pricing import lookup_pricing

logger = logging.getLogger(__name__)


#: Typed map from a runtime-fallback ``RuntimeErrorClass`` value (carried on a
#: ``runtime_switched`` event payload's ``cause`` field) onto the canonical
#: :class:`IncidentCause` member. Keyed on the enum *value* string so a
#: ``runtime_switched`` envelope whose ``cause`` is already the typed
#: ``RuntimeErrorClass`` string resolves without prose normalisation.
_RUNTIME_ERROR_CLASS_TO_CAUSE: dict[str, IncidentCause] = {
    "RUNTIME_RATE_LIMIT": IncidentCause.RUNTIME_RATE_LIMIT,
    "RUNTIME_SERVER_ERROR": IncidentCause.RUNTIME_SERVER_ERROR,
    "RUNTIME_TIMEOUT": IncidentCause.RUNTIME_TIMEOUT,
    "RUNTIME_API_ERROR": IncidentCause.RUNTIME_API_ERROR,
    "RUNTIME_AUTH_ERROR": IncidentCause.RUNTIME_AUTH_ERROR,
}

#: Typed map from a closed ``EventKind`` / ``event_type`` tag onto the
#: :class:`IncidentCause` the incident-bearing event projects to. Keyed on the
#: closed event-type literal (never on prose), so classification is a dict
#: lookup, not a substring scan. ``runtime_switched`` is intentionally absent:
#: its cause is the per-row ``RuntimeErrorClass`` resolved via
#: :data:`_RUNTIME_ERROR_CLASS_TO_CAUSE`.
_EVENT_TYPE_TO_CAUSE: dict[str, IncidentCause] = {
    "runtime_unavailable": IncidentCause.RUNTIME_UNAVAILABLE,
    "runtime_auth_failed": IncidentCause.RUNTIME_AUTH_ERROR,
    "session_failover": IncidentCause.SESSION_FAILOVER,
    "session_handle_pruned": IncidentCause.SESSION_HANDLE_PRUNED,
    "cache_mislayer_alarm": IncidentCause.CACHE_MISLAYER,
    "subprocess_oom_killed": IncidentCause.DAEMON_SUBPROCESS_OOM,
    "subscription_dropped": IncidentCause.DAEMON_SUBSCRIPTION_DROPPED,
    "wal_recovery": IncidentCause.DAEMON_WAL_RECOVERY,
}

#: The closed set of event-type tags this aggregator classifies into a
#: :class:`TelemetryIncident`. The union of the runtime-switch tag and the
#: directly-mapped tags above; an event whose ``event_type`` is absent from
#: this set is not an incident and is skipped by :func:`incident_from_event`.
_INCIDENT_EVENT_TYPES: frozenset[str] = frozenset(
    {"runtime_switched", *_EVENT_TYPE_TO_CAUSE},
)

#: Default severity per :class:`IncidentCause` (C09 §5.10). Synthesised
#: incidents adopt this when the source payload carries no explicit severity.
#: A cause absent from this map defaults to :attr:`IncidentSeverity.MEDIUM`.
_CAUSE_DEFAULT_SEVERITY: dict[IncidentCause, IncidentSeverity] = {
    IncidentCause.RUNTIME_RATE_LIMIT: IncidentSeverity.LOW,
    IncidentCause.RUNTIME_SERVER_ERROR: IncidentSeverity.MEDIUM,
    IncidentCause.RUNTIME_TIMEOUT: IncidentSeverity.MEDIUM,
    IncidentCause.RUNTIME_API_ERROR: IncidentSeverity.MEDIUM,
    IncidentCause.RUNTIME_AUTH_ERROR: IncidentSeverity.CRITICAL,
    IncidentCause.RUNTIME_UNAVAILABLE: IncidentSeverity.HIGH,
    IncidentCause.SESSION_FAILOVER: IncidentSeverity.LOW,
    IncidentCause.SESSION_HANDLE_PRUNED: IncidentSeverity.LOW,
    IncidentCause.CACHE_MISLAYER: IncidentSeverity.MEDIUM,
    IncidentCause.DAEMON_SUBPROCESS_OOM: IncidentSeverity.HIGH,
    IncidentCause.DAEMON_SUBSCRIPTION_DROPPED: IncidentSeverity.MEDIUM,
    IncidentCause.DAEMON_WAL_RECOVERY: IncidentSeverity.CRITICAL,
}


def price_session(session: TelemetrySession) -> Decimal:
    """Price a session's token counts through the embedded PRICING snapshot.

    Source adapters leave ``total_cost_usd`` at its ``Decimal("0")`` default
    (they do not know per-token rates); the aggregator prices it here so the
    M02 ``eawf_cost_usd_total`` counter and the M08-M10 burn-rate gauges read
    a real cost. The cost is the per-direction token sum:

    * input tokens at ``input_per_token``;
    * output tokens at ``output_per_token``;
    * cache-read tokens at ``cache_read_per_token``;
    * cache-write tokens at ``cache_write_5m_per_token``.

    The session model carries a single ``total_cache_write`` (cache-creation
    tokens) with no TTL split, so cache writes price at the 5m rate — the
    default ephemeral cache TTL the adapters read from
    ``cache_creation_input_tokens``.

    A session with no billable tokens prices to ``Decimal("0")`` without a
    snapshot lookup, so a zero-token row never depends on pricing being
    present. A non-zero token row whose ``model_primary`` is missing or
    absent from the snapshot is skipped (logged, priced ``Decimal("0")``)
    rather than raising, so an unknown model never crashes a projection.

    Args:
        session: A session row (project-stamped or not; pricing reads only
            its token counts and ``model_primary``).

    Returns:
        The session's USD cost as a :class:`~decimal.Decimal`; ``Decimal("0")``
        for a zero-token row or an unpriceable model.
    """
    billable = (
        session.total_input_tokens
        + session.total_output_tokens
        + session.total_cache_read
        + session.total_cache_write
    )
    if billable <= 0:
        return Decimal("0")
    model = session.model_primary
    pricing = lookup_pricing(model) if model else None
    if pricing is None:
        logger.warning(
            f"price_session session={session.session_id!r} model={model!r} "
            f"unpriced billable_tokens={billable}"
        )
        return Decimal("0")
    return (
        Decimal(session.total_input_tokens) * pricing.input_per_token
        + Decimal(session.total_output_tokens) * pricing.output_per_token
        + Decimal(session.total_cache_read) * pricing.cache_read_per_token
        + Decimal(session.total_cache_write) * pricing.cache_write_5m_per_token
    )


def roll_session(session: TelemetrySession, *, project_id: str) -> TelemetrySession:
    """Stamp a source session row with its project and price its cost.

    Per-runtime source adapters yield a
    :class:`~eawf.telemetry.models.TelemetrySession` with an empty
    ``project_id`` and a ``Decimal("0")`` ``total_cost_usd`` (they know
    neither the project hash nor the per-token rates); the aggregator stamps
    the project and prices the cost so the projector can upsert a
    fully-keyed, fully-priced row.

    Args:
        session: A session row yielded by a per-runtime source adapter.
        project_id: The owning project's id (``sha256(repo_path)[:12]``).

    Returns:
        A copy of *session* with ``project_id`` set and ``total_cost_usd``
        priced through :func:`price_session`.

    Raises:
        ValueError: When *project_id* is empty.
    """
    if not project_id:
        raise ValueError(f"project_id must be non-empty: {project_id!r}")
    return session.model_copy(
        update={"project_id": project_id, "total_cost_usd": price_session(session)}
    )


def classify_event_cause(envelope: Envelope) -> IncidentCause | None:
    """Resolve the typed :class:`IncidentCause` for an event-kind envelope.

    Classification is a typed lookup, never a substring scan:

    * a ``runtime_switched`` event resolves its cause from the typed
      ``RuntimeErrorClass`` value carried on the payload's ``cause`` field
      (via :data:`_RUNTIME_ERROR_CLASS_TO_CAUSE`); an unrecognised /
      absent ``cause`` falls back to
      :attr:`IncidentCause.RUNTIME_UNAVAILABLE`;
    * any other incident-bearing event resolves its cause from the closed
      ``event_type`` tag (via :data:`_EVENT_TYPE_TO_CAUSE`).

    Args:
        envelope: An event-kind store envelope.

    Returns:
        The resolved :class:`IncidentCause`, or ``None`` when the envelope
        is not an incident-bearing event.
    """
    event_type = envelope.payload.get("event_type")
    if not isinstance(event_type, str) or event_type not in _INCIDENT_EVENT_TYPES:
        return None
    if event_type == "runtime_switched":
        raw_cause = envelope.payload.get("cause")
        if isinstance(raw_cause, str):
            return _RUNTIME_ERROR_CLASS_TO_CAUSE.get(raw_cause, IncidentCause.RUNTIME_UNAVAILABLE)
        return IncidentCause.RUNTIME_UNAVAILABLE
    return _EVENT_TYPE_TO_CAUSE[event_type]


def default_severity_for(cause: IncidentCause) -> IncidentSeverity:
    """Return the default :class:`IncidentSeverity` for *cause* (C09 §5.10)."""
    return _CAUSE_DEFAULT_SEVERITY.get(cause, IncidentSeverity.MEDIUM)


def incident_from_envelope(envelope: Envelope) -> TelemetryIncident | None:
    """Fold an incident-bearing envelope into a :class:`TelemetryIncident`.

    Two envelope shapes carry an incident:

    * an ``incident``-kind envelope whose payload already holds a typed
      :class:`IncidentCause` + :class:`IncidentSeverity` — adopted
      verbatim (no reclassification);
    * an ``event``-kind envelope whose ``event_type`` is one of the
      incident-bearing tags — classified through the typed lookups in
      :func:`classify_event_cause`, with a default severity per cause.

    Any other envelope yields ``None`` (it is not an incident).

    Args:
        envelope: A store envelope from the event / incident JSONL stores.

    Returns:
        The synthesised :class:`TelemetryIncident`, or ``None`` when the
        envelope does not describe an incident.
    """
    if envelope.kind is StoreKind.INCIDENT:
        return _incident_from_incident_envelope(envelope)
    if envelope.kind is StoreKind.EVENT:
        return _incident_from_event_envelope(envelope)
    return None


def _incident_from_incident_envelope(envelope: Envelope) -> TelemetryIncident:
    """Adopt the typed cause / severity of an ``incident``-kind envelope.

    The :class:`~eawf.store.kinds.incident.IncidentPayload` already carries
    a closed :class:`IncidentCause` + :class:`IncidentSeverity`, so the row
    is built directly off those typed members.
    """
    payload = envelope.payload
    cause = IncidentCause(payload["cause"])
    severity = IncidentSeverity(payload["severity"])
    return TelemetryIncident(
        incident_id=envelope.id,
        severity=severity,
        cause=cause,
        ts=envelope.created_at,
        summary=envelope.summary,
        wave_id=_str_or_none(payload.get("wave_id")),
        attempt_id=_str_or_none(payload.get("attempt_id")),
    )


def _incident_from_event_envelope(envelope: Envelope) -> TelemetryIncident | None:
    """Classify an event-kind envelope into an incident, or ``None``.

    Returns ``None`` for an event whose ``event_type`` is not in the
    incident-bearing set; otherwise builds the row from the typed cause
    resolved by :func:`classify_event_cause` and the per-cause default
    severity.
    """
    cause = classify_event_cause(envelope)
    if cause is None:
        return None
    payload = envelope.payload
    ts = _ts_or(payload.get("timestamp"), envelope.created_at)
    return TelemetryIncident(
        incident_id=envelope.id,
        severity=default_severity_for(cause),
        cause=cause,
        ts=ts,
        summary=envelope.summary,
        wave_id=_str_or_none(payload.get("wave_id") or payload.get("trace_wave_id")),
        attempt_id=_str_or_none(payload.get("attempt_id") or payload.get("trace_attempt_id")),
    )


def _str_or_none(raw: object) -> str | None:
    """Return *raw* as a non-empty string, else ``None``."""
    return raw if isinstance(raw, str) and raw else None


def _ts_or(raw: object, fallback: datetime) -> datetime:
    """Parse *raw* as an ISO-8601 timestamp, else return *fallback*."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    return fallback


__all__ = [
    "classify_event_cause",
    "default_severity_for",
    "incident_from_envelope",
    "price_session",
    "roll_session",
]
