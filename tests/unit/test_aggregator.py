"""Unit tests for the telemetry aggregator (P27-I01-W15).

Covers the two load-bearing guarantees of the aggregator:

- **Session rolling** — :func:`roll_session` stamps a source session row
  with its owning ``project_id`` and fail-fasts on an empty project id.
- **Typed incident classification** — incident causes are resolved through
  the closed :class:`~eawf.state.enums.IncidentCause` lookups keyed on the
  ``event_type`` / ``RuntimeErrorClass`` enumerations, **never** by
  substring-matching prose. The tests prove that a ``runtime_switched``
  event whose ``cause`` is a typed ``RuntimeErrorClass`` resolves to the
  matching ``IncidentCause``, that a prose ``summary`` mentioning a
  different cause does *not* change the classification, and that an
  ``incident``-kind envelope's already-typed cause is adopted verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.state.enums import IncidentCause, IncidentSeverity, StoreKind
from eawf.store.envelope import Envelope
from eawf.telemetry.aggregator import (
    classify_event_cause,
    default_severity_for,
    incident_from_envelope,
    roll_session,
)
from eawf.telemetry.models import TelemetrySession

_TS = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _session(session_id: str = "sess-1", project_id: str = "") -> TelemetrySession:
    return TelemetrySession(
        session_id=session_id,
        project_id=project_id,
        runtime="claude",
        wave_id=None,
        attempt_id=None,
        session_log_path="opaque://log/sess-1",
        started_at=_TS,
        ended_at=_TS,
        duration_ms=0,
        model_primary="claude-opus",
        end_marker="other",
    )


def _event_envelope(
    *,
    env_id: str = "EV-1",
    payload: dict[str, Any],
    summary: str = "an event",
) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id="ABC",
        created_at=_TS,
        updated_at=None,
        summary=summary,
        payload=payload,
    )


def _incident_envelope(
    *,
    env_id: str = "INC-1",
    cause: IncidentCause,
    severity: IncidentSeverity,
    summary: str = "an incident",
    wave_id: str | None = None,
) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.INCIDENT,
        scope_id="ABC",
        created_at=_TS,
        summary=summary,
        payload={
            "severity": severity.value,
            "cause": cause.value,
            "timeline": [],
            "corrective_action_ids": [],
            "wave_id": wave_id,
        },
    )


# --------------------------------------------------------------------------- #
# roll_session — stamps project_id, fail-fasts on empty.
# --------------------------------------------------------------------------- #


def test_roll_session_stamps_project_id() -> None:
    rolled = roll_session(_session(), project_id="proj123")
    assert rolled.project_id == "proj123"
    # Source row is left untouched (pure copy).
    assert _session().project_id == ""


def test_roll_session_preserves_all_other_fields() -> None:
    src = _session(session_id="sess-x")
    rolled = roll_session(src, project_id="proj123")
    assert rolled.session_id == "sess-x"
    assert rolled.runtime == "claude"
    assert rolled.model_primary == "claude-opus"


def test_roll_session_empty_project_id_raises() -> None:
    with pytest.raises(ValueError, match="project_id must be non-empty"):
        roll_session(_session(), project_id="")


# --------------------------------------------------------------------------- #
# classify_event_cause — typed lookup, not substring matching.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("runtime_error_class", "expected"),
    [
        ("RUNTIME_RATE_LIMIT", IncidentCause.RUNTIME_RATE_LIMIT),
        ("RUNTIME_SERVER_ERROR", IncidentCause.RUNTIME_SERVER_ERROR),
        ("RUNTIME_TIMEOUT", IncidentCause.RUNTIME_TIMEOUT),
        ("RUNTIME_API_ERROR", IncidentCause.RUNTIME_API_ERROR),
        ("RUNTIME_AUTH_ERROR", IncidentCause.RUNTIME_AUTH_ERROR),
    ],
)
def test_classify_runtime_switch_uses_typed_error_class(
    runtime_error_class: str, expected: IncidentCause
) -> None:
    env = _event_envelope(
        payload={
            "event_type": "runtime_switched",
            "cause": runtime_error_class,
            "timestamp": _TS.isoformat(),
        },
    )
    assert classify_event_cause(env) == expected


def test_classify_ignores_summary_prose() -> None:
    # Summary prose names a *different* cause; classification must follow
    # the typed ``cause`` field, never the prose.
    env = _event_envelope(
        payload={
            "event_type": "runtime_switched",
            "cause": "RUNTIME_RATE_LIMIT",
            "timestamp": _TS.isoformat(),
        },
        summary="server returned a 500 timeout auth error daemon oom",
    )
    assert classify_event_cause(env) == IncidentCause.RUNTIME_RATE_LIMIT


def test_classify_runtime_switch_unknown_cause_falls_back() -> None:
    env = _event_envelope(
        payload={
            "event_type": "runtime_switched",
            "cause": "NOT_A_KNOWN_CLASS",
            "timestamp": _TS.isoformat(),
        },
    )
    assert classify_event_cause(env) == IncidentCause.RUNTIME_UNAVAILABLE


def test_classify_runtime_switch_missing_cause_falls_back() -> None:
    env = _event_envelope(payload={"event_type": "runtime_switched"})
    assert classify_event_cause(env) == IncidentCause.RUNTIME_UNAVAILABLE


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("session_failover", IncidentCause.SESSION_FAILOVER),
        ("session_handle_pruned", IncidentCause.SESSION_HANDLE_PRUNED),
        ("cache_mislayer_alarm", IncidentCause.CACHE_MISLAYER),
        ("subprocess_oom_killed", IncidentCause.DAEMON_SUBPROCESS_OOM),
        ("wal_recovery", IncidentCause.DAEMON_WAL_RECOVERY),
        ("runtime_unavailable", IncidentCause.RUNTIME_UNAVAILABLE),
        ("runtime_auth_failed", IncidentCause.RUNTIME_AUTH_ERROR),
    ],
)
def test_classify_event_type_uses_typed_lookup(event_type: str, expected: IncidentCause) -> None:
    env = _event_envelope(payload={"event_type": event_type})
    assert classify_event_cause(env) == expected


def test_classify_non_incident_event_returns_none() -> None:
    env = _event_envelope(payload={"event_type": "wave_closed"})
    assert classify_event_cause(env) is None


def test_classify_missing_event_type_returns_none() -> None:
    env = _event_envelope(payload={"actor": "cli"})
    assert classify_event_cause(env) is None


# --------------------------------------------------------------------------- #
# default_severity_for.
# --------------------------------------------------------------------------- #


def test_default_severity_known_cause() -> None:
    assert default_severity_for(IncidentCause.RUNTIME_AUTH_ERROR) == IncidentSeverity.CRITICAL
    assert default_severity_for(IncidentCause.RUNTIME_RATE_LIMIT) == IncidentSeverity.LOW


def test_default_severity_unknown_cause_defaults_medium() -> None:
    assert default_severity_for(IncidentCause.PLUGIN_DRIFT) == IncidentSeverity.MEDIUM


# --------------------------------------------------------------------------- #
# incident_from_envelope.
# --------------------------------------------------------------------------- #


def test_incident_from_event_envelope_classifies() -> None:
    env = _event_envelope(
        env_id="EV-switch",
        payload={
            "event_type": "runtime_switched",
            "cause": "RUNTIME_TIMEOUT",
            "timestamp": _TS.isoformat(),
            "wave_id": "W03",
            "attempt_id": "a2",
        },
        summary="switched claude to codex",
    )
    incident = incident_from_envelope(env)
    assert incident is not None
    assert incident.incident_id == "EV-switch"
    assert incident.cause == IncidentCause.RUNTIME_TIMEOUT
    assert incident.severity == IncidentSeverity.MEDIUM
    assert incident.wave_id == "W03"
    assert incident.attempt_id == "a2"
    assert incident.ts == _TS


def test_incident_from_event_envelope_uses_trace_fields() -> None:
    env = _event_envelope(
        payload={
            "event_type": "session_failover",
            "trace_wave_id": "W07",
            "trace_attempt_id": "fresh-1",
        },
    )
    incident = incident_from_envelope(env)
    assert incident is not None
    assert incident.cause == IncidentCause.SESSION_FAILOVER
    assert incident.wave_id == "W07"
    assert incident.attempt_id == "fresh-1"


def test_incident_from_non_incident_event_returns_none() -> None:
    env = _event_envelope(payload={"event_type": "wave_closed"})
    assert incident_from_envelope(env) is None


def test_incident_from_incident_envelope_adopts_typed_cause() -> None:
    env = _incident_envelope(
        env_id="INC-7",
        cause=IncidentCause.WORKTREE_CHERRY_PICK_CONFLICT,
        severity=IncidentSeverity.HIGH,
        summary="cherry-pick conflict on W04",
        wave_id="W04",
    )
    incident = incident_from_envelope(env)
    assert incident is not None
    assert incident.incident_id == "INC-7"
    assert incident.cause == IncidentCause.WORKTREE_CHERRY_PICK_CONFLICT
    assert incident.severity == IncidentSeverity.HIGH
    assert incident.summary == "cherry-pick conflict on W04"
    assert incident.wave_id == "W04"


def test_incident_from_incident_envelope_preserves_legacy_sentinel() -> None:
    env = _incident_envelope(
        cause=IncidentCause.LEGACY_FREE_TEXT,
        severity=IncidentSeverity.LOW,
    )
    incident = incident_from_envelope(env)
    assert incident is not None
    assert incident.cause == IncidentCause.LEGACY_FREE_TEXT
