"""Golden + behavioural tests for the telemetry exporter (P27-I01-W16).

The exporter is pure and deterministic: a seeded fixture DB renders to
byte-identical ``prom`` / ``json`` / ``csv`` documents on every run. These
tests:

1. Seed a fixed set of :class:`~eawf.observability.telemetry.models.TelemetrySession` +
   :class:`~eawf.observability.telemetry.models.TelemetryIncident` rows into a SQLite
   metrics store.
2. Build a snapshot and render each format.
3. Assert the rendered bytes match the committed golden under
   ``tests/golden/metrics_export/``.

Regenerate the goldens after an intentional shape change with::

    EAWF_REGEN_GOLDEN=1 uv run pytest tests/unit/test_metrics_export.py

The regen path writes the current output back to the golden file and the
assertion trivially passes; commit the refreshed bytes and drop the env
var on the next run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity
from eawf.observability.telemetry.exporter import (
    build_snapshot,
    render,
)
from eawf.observability.telemetry.models import TelemetryIncident, TelemetrySession
from eawf.observability.telemetry.store import SqliteMetricsStore

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden" / "metrics_export"
_SCOPE = "repo/eawf"
_TS = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _seed_store(db_path: Path) -> SqliteMetricsStore:
    """Open a SQLite store at *db_path* and seed the fixture rows.

    The fixture mirrors the C09 §5.9.5 worked example (Claude session with
    real-shaped token + cost figures) plus a second runtime with no cache
    activity, and two incidents differing in severity + cause.
    """
    store = SqliteMetricsStore(db_path)
    store.init_schema()
    for session in _fixture_sessions():
        store.upsert("telemetry_sessions", session)
    for incident in _fixture_incidents():
        store.upsert("telemetry_incidents", incident)
    store.commit()
    return store


def _fixture_sessions() -> list[TelemetrySession]:
    return [
        TelemetrySession(
            session_id="s1",
            project_id=_SCOPE,
            runtime="claude",
            wave_id="P27-I01-W16",
            attempt_id="a1",
            session_log_path="claude/s1.jsonl",
            started_at=_TS,
            ended_at=_TS,
            duration_ms=120000,
            model_primary="claude-opus-4-7",
            total_input_tokens=812440,
            total_output_tokens=142810,
            total_cache_read=6108002,
            total_cache_write=348221,
            total_cost_usd=Decimal("7.31"),
            turn_count=40,
            tool_call_count=120,
            error_count=2,
            denial_count=1,
            interrupt_count=0,
            compaction_count=3,
            subagent_dispatch_count=5,
            end_marker="clean_stop",
        ),
        TelemetrySession(
            session_id="s2",
            project_id=_SCOPE,
            runtime="codex",
            wave_id=None,
            attempt_id=None,
            session_log_path="codex/s2.jsonl",
            started_at=_TS,
            ended_at=_TS,
            duration_ms=60000,
            model_primary="codex-model",
            total_input_tokens=10000,
            total_output_tokens=5000,
            total_cache_read=0,
            total_cache_write=0,
            total_cost_usd=Decimal("0.42"),
            turn_count=10,
            tool_call_count=20,
            error_count=0,
            denial_count=0,
            interrupt_count=0,
            compaction_count=1,
            subagent_dispatch_count=2,
            end_marker="clean_stop",
        ),
    ]


def _fixture_incidents() -> list[TelemetryIncident]:
    return [
        TelemetryIncident(
            incident_id="i1",
            severity=IncidentSeverity.MEDIUM,
            cause=IncidentCause.RUNTIME_TIMEOUT,
            ts=_TS,
            summary="dispatch timed out",
            wave_id="P27-I01-W16",
            attempt_id="a1",
        ),
        TelemetryIncident(
            incident_id="i2",
            severity=IncidentSeverity.CRITICAL,
            cause=IncidentCause.RUNTIME_AUTH_ERROR,
            ts=_TS,
            summary="auth failure",
        ),
    ]


def _render_fixture(tmp_path: Path, fmt: str) -> str:
    store = _seed_store(tmp_path / "telemetry.db")
    try:
        snapshot = build_snapshot(store, scope=_SCOPE)
    finally:
        store.close()
    return render(snapshot, fmt=fmt)


def _assert_golden(actual: str, golden_name: str) -> None:
    golden_path = _GOLDEN_DIR / golden_name
    if os.environ.get("EAWF_REGEN_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, f"golden drift vs {golden_name}"


@pytest.mark.parametrize(
    ("fmt", "golden_name"),
    [
        ("prom", "fixture.prom"),
        ("json", "fixture.json"),
        ("csv", "fixture.csv"),
    ],
)
def test_export_format_matches_golden(tmp_path: Path, fmt: str, golden_name: str) -> None:
    """Each export format renders byte-identically to its committed golden."""
    actual = _render_fixture(tmp_path, fmt)
    _assert_golden(actual, golden_name)


def test_export_is_deterministic_across_runs(tmp_path: Path) -> None:
    """Rendering the same seeded DB twice yields byte-identical output."""
    first = _render_fixture(tmp_path / "a", "prom")
    second = _render_fixture(tmp_path / "b", "prom")
    assert first == second


def test_export_empty_db_renders_all_families(tmp_path: Path) -> None:
    """An empty store still emits every metric family header (no samples)."""
    store = SqliteMetricsStore(tmp_path / "empty.db")
    store.init_schema()
    try:
        snapshot = build_snapshot(store, scope=_SCOPE)
    finally:
        store.close()
    prom = render(snapshot, fmt="prom")
    assert "# HELP eawf_tokens_total" in prom
    assert "# HELP eawf_incidents_total" in prom
    # subagent_dispatch always emits a zero counter even with no sessions.
    assert "eawf_subagent_dispatch_total 0" in prom


def test_export_cache_hit_ratio_matches_spec_example(tmp_path: Path) -> None:
    """The Claude cache-hit-ratio gauge equals the C09 §5.9.5 worked value (0.946)."""
    store = _seed_store(tmp_path / "telemetry.db")
    try:
        snapshot = build_snapshot(store, scope=_SCOPE)
    finally:
        store.close()
    ratio_family = next(f for f in snapshot.families if f.name == "eawf_cache_hit_ratio")
    claude_sample = next(s for s in ratio_family.samples if ("runtime", "claude") in s.labels)
    assert claude_sample.value == Decimal("0.946")


def test_render_unknown_format_raises(tmp_path: Path) -> None:
    """An unrecognised format string fails fast with ``ValueError``."""
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    try:
        snapshot = build_snapshot(store, scope=_SCOPE)
    finally:
        store.close()
    with pytest.raises(ValueError, match="unknown export format"):
        render(snapshot, fmt="xml")  # type: ignore[arg-type]
