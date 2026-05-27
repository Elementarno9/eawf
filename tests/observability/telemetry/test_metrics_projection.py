"""Tests for the TUI metrics projection aggregator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import State
from eawf.observability.telemetry.metrics_projection import (
    METRICS_PROJECTION_SCHEMA_VERSION,
    MetricsProjection,
    compute_metrics_projection,
)
from eawf.observability.telemetry.models import TelemetryRuntimeSwitch, TelemetrySession
from eawf.observability.telemetry.store import SqliteMetricsStore

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
_SCOPE = "urn:eawf:v1:state:QR"


def _state() -> State:
    """Return a state with closed waves across two effort buckets."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": _SCOPE,
        "updated_at": "2026-05-22T12:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
            "weekly_eu_target": 4.0,
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P01": {
                "id": "P01",
                "scope_id": "QR",
                "title": "Phase 1",
                "status": "closed",
                "iter_ids": ["P01-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-20T12:00:00Z",
                "closed_at": "2026-05-22T12:00:00Z",
            }
        },
        "iters": {
            "P01-I01": {
                "id": "P01-I01",
                "phase_id": "P01",
                "title": "Iter 1",
                "status": "closed",
                "wave_ids": ["P01-I01-W01", "P01-I01-W02"],
                "opened_at": "2026-05-20T12:00:00Z",
                "closed_at": "2026-05-22T12:00:00Z",
            }
        },
        "waves": {
            "P01-I01-W01": {
                "id": "P01-I01-W01",
                "iter_id": "P01-I01",
                "title": "Wave 1",
                "status": "closed",
                "deps": [],
                "file_scopes": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "opened_at": "2026-05-20T12:00:00Z",
                "closed_at": "2026-05-20T12:30:00Z",
            },
            "P01-I01-W02": {
                "id": "P01-I01-W02",
                "iter_id": "P01-I01",
                "title": "Wave 2",
                "status": "closed",
                "deps": [],
                "file_scopes": [],
                "agent_role": "auditor",
                "effort_bucket": "L",
                "opened_at": "2026-05-21T12:00:00Z",
                "closed_at": "2026-05-21T13:00:00Z",
            },
        },
        "estimates": {
            "P01-I01-W01": {
                "id": "EST-P01-I01-W01",
                "scope_id": "P01-I01-W01",
                "expected_eu": 1.0,
                "pessimistic_eu": 2.0,
                "expected_minutes": 60.0,
                "pessimistic_minutes": 120.0,
                "display": "1.0 EU",
                "confidence": "medium",
                "current_store_record_id": "EST-REC-1",
                "updated_at": "2026-05-20T12:00:00Z",
            },
            "EST-P01-I01-W02": {
                "id": "EST-P01-I01-W02",
                "scope_id": "P01-I01-W02",
                "expected_eu": 2.0,
                "pessimistic_eu": 2.5,
                "expected_minutes": 120.0,
                "pessimistic_minutes": 150.0,
                "display": "2.0 EU",
                "confidence": "medium",
                "current_store_record_id": "EST-REC-2",
                "updated_at": "2026-05-21T12:00:00Z",
            },
        },
        "actuals": {
            "P01-I01-W01": {
                "id": "ACT-P01-I01-W01",
                "scope_id": "P01-I01-W01",
                "status": "done",
                "elapsed_eu": 1.5,
                "current_store_record_id": "ACT-REC-1",
                "updated_at": "2026-05-21T12:00:00Z",
            },
            "ACT-P01-I01-W02": {
                "id": "ACT-P01-I01-W02",
                "scope_id": "P01-I01-W02",
                "status": "done",
                "elapsed_eu": 3.0,
                "current_store_record_id": "ACT-REC-2",
                "updated_at": "2026-05-21T12:00:00Z",
            },
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _session(
    session_id: str,
    *,
    runtime: Literal["claude", "codex", "opencode"],
    started_at: datetime,
    project_id: str,
) -> TelemetrySession:
    """Return one telemetry session row."""
    return TelemetrySession(
        session_id=session_id,
        project_id=project_id,
        runtime=runtime,
        wave_id="P01-I01-W01",
        attempt_id="a1",
        session_log_path=f"{runtime}/{session_id}.jsonl",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=10),
        duration_ms=600000,
        model_primary=f"{runtime}-model",
        total_input_tokens=100,
        total_output_tokens=50,
        total_cache_read=80,
        total_cache_write=20,
        total_cost_usd=Decimal("0.01"),
        turn_count=3,
        tool_call_count=4,
        error_count=0,
        denial_count=0,
        interrupt_count=0,
        compaction_count=0,
        subagent_dispatch_count=1,
        end_marker="clean_stop",
    )


def _seed_store(tmp_path: Path) -> SqliteMetricsStore:
    """Return a store seeded with in-window and out-of-window rows."""
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    store.upsert(
        "telemetry_sessions",
        _session("s1", runtime="claude", started_at=_NOW - timedelta(days=1), project_id=_SCOPE),
    )
    store.upsert(
        "telemetry_sessions",
        _session("s2", runtime="codex", started_at=_NOW - timedelta(days=40), project_id=_SCOPE),
    )
    store.upsert(
        "telemetry_sessions",
        _session(
            "s3",
            runtime="opencode",
            started_at=_NOW - timedelta(days=1),
            project_id="urn:eawf:v1:state:OTHER",
        ),
    )
    store.upsert(
        "telemetry_runtime_switches",
        TelemetryRuntimeSwitch(
            wave_id="P01-I01-W01",
            attempt_id_from="a1",
            attempt_id_to="a2",
            runtime_from="claude",
            runtime_to="codex",
            cause="RUNTIME_TIMEOUT",
            ts=_NOW - timedelta(days=1),
        ),
    )
    store.upsert(
        "telemetry_runtime_switches",
        TelemetryRuntimeSwitch(
            wave_id="P01-I01-W01",
            attempt_id_from="a2",
            attempt_id_to="a3",
            runtime_from="codex",
            runtime_to="claude",
            cause="RUNTIME_RATE_LIMIT",
            ts=_NOW - timedelta(days=40),
        ),
    )
    store.commit()
    return store


def test_compute_metrics_projection_emits_typed_projection(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    try:
        projection = compute_metrics_projection(_state(), store=store, window="7d", now=_NOW)
    finally:
        store.close()

    assert isinstance(projection, MetricsProjection)
    assert projection.schema_version == METRICS_PROJECTION_SCHEMA_VERSION
    assert projection.variance.sample_count == 2
    assert projection.variance.variance_pct == pytest.approx(50.0)
    assert projection.weekly_burn.consumed_eu == pytest.approx(4.5)
    assert projection.wave_elapsed.sample_count == 2
    assert [row.agent_role.value for row in projection.per_role_calibration] == [
        "executor",
        "auditor",
    ]


def test_compute_metrics_projection_exposes_variance_by_bucket(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    try:
        projection = compute_metrics_projection(_state(), store=store, window="7d", now=_NOW)
    finally:
        store.close()

    buckets = {row.bucket.value: row for row in projection.variance_by_bucket}
    assert set(buckets) == {"M", "L"}
    assert buckets["M"].variance_pct == pytest.approx(50.0)
    assert buckets["M"].inside_pessimistic_share == pytest.approx(1.0)
    assert buckets["L"].inside_pessimistic_share == pytest.approx(0.0)
    assert buckets["L"].waves[0].wave_id == "P01-I01-W02"


def test_compute_metrics_projection_exposes_per_role_calibration(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    try:
        projection = compute_metrics_projection(_state(), store=store, window="7d", now=_NOW)
    finally:
        store.close()

    by_role = {row.agent_role.value: row.report for row in projection.per_role_calibration}
    executor_buckets = {row.bucket.value: row for row in by_role["executor"].buckets}
    auditor_buckets = {row.bucket.value: row for row in by_role["auditor"].buckets}

    assert executor_buckets["M"].fitted_eu == pytest.approx(1.5)
    assert executor_buckets["M"].sample_count == 1
    assert auditor_buckets["L"].fitted_eu == pytest.approx(3.0)
    assert auditor_buckets["L"].nudge is True


def test_compute_metrics_projection_filters_telemetry_by_scope_and_window(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    try:
        projection = compute_metrics_projection(_state(), store=store, window="7d", now=_NOW)
    finally:
        store.close()

    assert [row.runtime for row in projection.per_runtime_tokens] == ["claude"]
    assert projection.per_runtime_tokens[0].total_tokens == 250
    assert projection.cache_health[0].hit_ratio == pytest.approx(0.8)
    assert [(row.cause.value, row.count) for row in projection.switchover_frequency] == [
        ("RUNTIME_TIMEOUT", 1)
    ]


def test_compute_metrics_projection_without_store_keeps_state_tiles() -> None:
    projection = compute_metrics_projection(_state(), store=None, window="7d", now=_NOW)

    assert projection.variance.sample_count == 2
    assert projection.cache_health == ()
    assert projection.switchover_frequency == ()
    assert projection.per_runtime_tokens == ()


def test_metrics_projection_rejects_extra_keys() -> None:
    projection = compute_metrics_projection(_state(), store=None, window="7d", now=_NOW)
    payload = projection.model_dump(mode="json")
    payload["unexpected"] = "nope"

    with pytest.raises(ValidationError):
        MetricsProjection.model_validate(payload)
