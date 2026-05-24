"""Unit tests for the telemetry aggregator (P27-I01-W15).

Covers the two load-bearing guarantees of the aggregator:

- **Session rolling** — :func:`roll_session` stamps a source session row
  with its owning ``project_id`` and fail-fasts on an empty project id.
- **Typed incident classification** — incident causes are resolved through
  the closed :class:`~eawf.kernel.state.enums.IncidentCause` lookups keyed on the
  ``event_type`` / ``RuntimeErrorClass`` enumerations, **never** by
  substring-matching prose. The tests prove that a ``runtime_switched``
  event whose ``cause`` is a typed ``RuntimeErrorClass`` resolves to the
  matching ``IncidentCause``, that a prose ``summary`` mentioning a
  different cause does *not* change the classification, and that an
  ``incident``-kind envelope's already-typed cause is adopted verbatim.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.telemetry.aggregator import (
    classify_event_cause,
    default_severity_for,
    incident_from_envelope,
    price_session,
    roll_session,
)
from eawf.telemetry.models import TelemetryIncident, TelemetrySession
from eawf.telemetry.pricing import PRICING
from eawf.telemetry.projector import RebuildMode, SourceSpec, rebuild
from eawf.telemetry.store import SqliteMetricsStore

_TS = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _session(
    session_id: str = "sess-1",
    project_id: str = "",
    *,
    model_primary: str | None = "claude-opus",
    total_input_tokens: int = 0,
    total_output_tokens: int = 0,
    total_cache_read: int = 0,
    total_cache_write: int = 0,
) -> TelemetrySession:
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
        model_primary=model_primary,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cache_read=total_cache_read,
        total_cache_write=total_cache_write,
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
# price_session — token counts priced through the PRICING snapshot.
# --------------------------------------------------------------------------- #


def test_price_session_known_model_yields_nonzero_cost() -> None:
    row = PRICING["claude-opus-4-7"]
    session = _session(
        model_primary="claude-opus-4-7",
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cache_read=2000,
        total_cache_write=800,
    )
    expected = (
        Decimal(1000) * row.input_per_token
        + Decimal(500) * row.output_per_token
        + Decimal(2000) * row.cache_read_per_token
        + Decimal(800) * row.cache_write_5m_per_token
    )
    cost = price_session(session)
    assert cost == expected
    assert cost > Decimal("0")


def test_price_session_dated_model_resolves_via_prefix() -> None:
    # A dated variant is not a snapshot key; longest-prefix fallback prices it.
    row = PRICING["claude-opus-4-7"]
    session = _session(
        model_primary="claude-opus-4-7-20260514",
        total_input_tokens=1000,
    )
    assert price_session(session) == Decimal(1000) * row.input_per_token


def test_price_session_zero_token_row_is_zero_no_div_by_zero() -> None:
    # All token counts zero: cost is Decimal("0") with no division performed.
    session = _session(model_primary="claude-opus-4-7")
    cost = price_session(session)
    assert cost == Decimal("0")
    assert isinstance(cost, Decimal)


def test_price_session_zero_tokens_unknown_model_does_not_crash() -> None:
    # Zero tokens short-circuit before the snapshot lookup, so an unknown
    # model never matters for a zero-token row.
    session = _session(model_primary="totally-unknown-model")
    assert price_session(session) == Decimal("0")


def test_price_session_unknown_model_skips_and_flags(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A non-zero token row whose model is absent from the snapshot is skipped
    # (priced Decimal("0"), flagged) rather than raising.
    session = _session(
        session_id="sess-unknown",
        model_primary="gpt-4o",
        total_input_tokens=1000,
    )
    with caplog.at_level("WARNING"):
        cost = price_session(session)
    assert cost == Decimal("0")
    assert any("unpriced" in rec.message for rec in caplog.records)


def test_price_session_missing_model_skips_and_flags(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A non-zero token row with no model_primary at all is skipped, not crashed.
    session = _session(
        session_id="sess-nomodel",
        model_primary=None,
        total_output_tokens=42,
    )
    with caplog.at_level("WARNING"):
        cost = price_session(session)
    assert cost == Decimal("0")
    assert any("unpriced" in rec.message for rec in caplog.records)


def test_roll_session_prices_total_cost_usd() -> None:
    row = PRICING["claude-opus-4-7"]
    session = _session(
        model_primary="claude-opus-4-7",
        total_input_tokens=1000,
        total_output_tokens=500,
        total_cache_read=2000,
        total_cache_write=800,
    )
    rolled = roll_session(session, project_id="proj123")
    expected = (
        Decimal(1000) * row.input_per_token
        + Decimal(500) * row.output_per_token
        + Decimal(2000) * row.cache_read_per_token
        + Decimal(800) * row.cache_write_5m_per_token
    )
    assert rolled.total_cost_usd == expected
    assert rolled.total_cost_usd > Decimal("0")
    # Source row is left untouched (pure copy): cost stays at its default.
    assert session.total_cost_usd == Decimal("0")


def test_roll_session_zero_token_row_prices_zero() -> None:
    session = _session(model_primary="claude-opus-4-7")
    rolled = roll_session(session, project_id="proj123")
    assert rolled.total_cost_usd == Decimal("0")


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


# --------------------------------------------------------------------------- #
# Malformed incident-kind envelope — logged + skipped, never raised.
# --------------------------------------------------------------------------- #


def _malformed_incident_envelope(
    *,
    env_id: str = "INC-bad",
    payload: dict[str, Any],
    summary: str = "a malformed incident",
) -> Envelope:
    """Build an ``incident``-kind envelope with a caller-controlled payload."""
    return Envelope(
        id=env_id,
        kind=StoreKind.INCIDENT,
        scope_id="ABC",
        created_at=_TS,
        summary=summary,
        payload=payload,
    )


def test_incident_from_envelope_missing_cause_key_skips_not_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A KeyError on the absent ``cause`` field used to crash the whole
    # rebuild; the row is now logged and skipped (returns None).
    env = _malformed_incident_envelope(
        payload={"severity": IncidentSeverity.HIGH.value, "wave_id": "W04"},
    )
    with caplog.at_level("WARNING"):
        incident = incident_from_envelope(env)
    assert incident is None
    assert any("malformed" in rec.message for rec in caplog.records)


def test_incident_from_envelope_missing_severity_key_skips_not_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _malformed_incident_envelope(
        payload={"cause": IncidentCause.RUNTIME_TIMEOUT.value},
    )
    with caplog.at_level("WARNING"):
        incident = incident_from_envelope(env)
    assert incident is None
    assert any("malformed" in rec.message for rec in caplog.records)


def test_incident_from_envelope_bad_cause_value_skips_not_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A ValueError on a ``cause`` outside the closed enum is skipped, not raised.
    env = _malformed_incident_envelope(
        payload={"cause": "not-a-real-cause", "severity": IncidentSeverity.LOW.value},
    )
    with caplog.at_level("WARNING"):
        incident = incident_from_envelope(env)
    assert incident is None
    assert any("malformed" in rec.message for rec in caplog.records)


def test_incident_from_envelope_bad_severity_value_skips_not_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = _malformed_incident_envelope(
        payload={"cause": IncidentCause.RUNTIME_TIMEOUT.value, "severity": "bogus"},
    )
    with caplog.at_level("WARNING"):
        incident = incident_from_envelope(env)
    assert incident is None
    assert any("malformed" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# Rebuild-path contract — one malformed incident skipped, good rows flow.
# --------------------------------------------------------------------------- #


def _incident_line(
    env_id: str,
    *,
    cause: str | None = IncidentCause.RUNTIME_TIMEOUT.value,
    severity: str | None = IncidentSeverity.MEDIUM.value,
) -> str:
    """Serialise one ``incident``-kind envelope JSONL line.

    A ``None`` *cause* / *severity* omits that key, producing the
    missing-key malformed shape a rebuild must skip rather than abort on.
    """
    payload: dict[str, Any] = {}
    if cause is not None:
        payload["cause"] = cause
    if severity is not None:
        payload["severity"] = severity
    env = Envelope(
        id=env_id,
        kind=StoreKind.INCIDENT,
        scope_id="ABC",
        created_at=_TS,
        summary=f"incident {env_id}",
        payload=payload,
    )
    return env.model_dump_json() + "\n"


class _IncidentFileSource:
    """In-test incident source: discovers ``incidents.jsonl``, yields envelopes."""

    source_name = "test_incidents"

    def discover(self, root: Path) -> Iterator[Path]:
        path = root / "incidents.jsonl"
        if path.is_file():
            yield path

    def iter_rows(self, path: Path) -> Iterator[Envelope]:
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line:
                    yield Envelope.model_validate_json(line)


def _open_store(tmp_path: Path) -> SqliteMetricsStore:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    return store


def _incident_rows(store: SqliteMetricsStore) -> list[TelemetryIncident]:
    rows = store.fetch_all("telemetry_incidents", TelemetryIncident)
    return [r for r in rows if isinstance(r, TelemetryIncident)]


def test_rebuild_skips_one_malformed_incident_and_projects_good_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Canonical success criterion: a rebuild over a log with ONE malformed
    # incident (missing ``cause`` key) completes, projects every good row,
    # and logs+skips the bad one — no exception propagates.
    path = tmp_path / "incidents.jsonl"
    path.write_text(
        _incident_line("INC-1")
        + _incident_line("INC-bad", cause=None)
        + _incident_line("INC-2")
        + _incident_line("INC-3"),
        encoding="utf-8",
    )
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_IncidentFileSource(), root=tmp_path, project_id="p1")

    with caplog.at_level("WARNING"):
        report = rebuild(store, [spec], mode=RebuildMode.FULL)

    assert report.incidents == 3
    ids = {i.incident_id for i in _incident_rows(store)}
    assert ids == {"INC-1", "INC-2", "INC-3"}
    assert any("malformed" in rec.message for rec in caplog.records)
    store.close()


def test_rebuild_skips_bad_severity_value_and_projects_good_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A bad VALUE (severity outside the closed enum) is skipped, not crashed.
    path = tmp_path / "incidents.jsonl"
    path.write_text(
        _incident_line("INC-1")
        + _incident_line("INC-bad", severity="bogus")
        + _incident_line("INC-2"),
        encoding="utf-8",
    )
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_IncidentFileSource(), root=tmp_path, project_id="p1")

    with caplog.at_level("WARNING"):
        report = rebuild(store, [spec], mode=RebuildMode.FULL)

    assert report.incidents == 2
    ids = {i.incident_id for i in _incident_rows(store)}
    assert ids == {"INC-1", "INC-2"}
    assert any("malformed" in rec.message for rec in caplog.records)
    store.close()


def test_rebuild_all_good_incidents_unaffected(tmp_path: Path) -> None:
    # Boundary: a log with no malformed rows projects every incident
    # (no regression from the guard).
    path = tmp_path / "incidents.jsonl"
    path.write_text(
        _incident_line("INC-1") + _incident_line("INC-2") + _incident_line("INC-3"),
        encoding="utf-8",
    )
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_IncidentFileSource(), root=tmp_path, project_id="p1")

    report = rebuild(store, [spec], mode=RebuildMode.FULL)

    assert report.incidents == 3
    assert {i.incident_id for i in _incident_rows(store)} == {"INC-1", "INC-2", "INC-3"}
    store.close()
