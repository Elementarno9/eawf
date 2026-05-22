"""Unit tests for the telemetry metrics store (P27-I01-W12).

Covers the four load-bearing guarantees of the wave:

- **DDL from models** — every column emitted by :func:`render_ddl` is
  derived from a Pydantic row-model field, and the SQL type-map honours the
  small KISS contract (bool/int → INTEGER, Decimal/datetime/str/enum → TEXT,
  float → REAL).
- **Round-trip** — rows written through :meth:`AbstractMetricsStore.upsert`
  read back equal through :meth:`AbstractMetricsStore.fetch_all`, with
  ``Decimal``, ``datetime``, ``bool``, and enum fields surviving the
  encode/validate cycle.
- **Idempotent ``init_schema``** — a second call no-ops (no error, no
  duplicate schema-meta row).
- **ImportError fallback** — :func:`open_store` with ``db_kind="duckdb"``
  falls back to a SQLite store when ``import duckdb`` raises ``ImportError``
  (forced via a monkeypatched ``builtins.__import__``).
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.state.enums import IncidentCause, IncidentSeverity
from eawf.telemetry.models import (
    TelemetryCompaction,
    TelemetryIncident,
    TelemetryProject,
    TelemetrySchemaMeta,
    TelemetrySession,
    TelemetryToolCall,
)
from eawf.telemetry.store import (
    TABLES,
    AbstractMetricsStore,
    SqliteMetricsStore,
    open_store,
)
from eawf.telemetry.store.base import (
    SCHEMA_VERSION,
    column_sql_type,
    render_create_table,
    render_ddl,
)


def _project_row() -> TelemetryProject:
    return TelemetryProject(
        project_id="abc123",
        cwd="repo/eawf",
        repo_name="eawf",
        first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        last_seen=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
        has_settings_local=True,
        has_agents_md=True,
        has_eawf_state=False,
    )


def _session_row() -> TelemetrySession:
    return TelemetrySession(
        session_id="sess-1",
        project_id="abc123",
        runtime="claude",
        wave_id="P27-I01-W12",
        attempt_id="a1",
        session_log_path="opaque://log/sess-1",
        started_at=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
        ended_at=datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        duration_ms=1_800_000,
        model_primary="claude-opus-4-7",
        total_input_tokens=1000,
        total_output_tokens=200,
        total_cost_usd=Decimal("7.314159"),
        end_marker="clean_stop",
    )


def _tool_call_row() -> TelemetryToolCall:
    return TelemetryToolCall(
        session_id="sess-1",
        turn_idx=3,
        tool_use_id="tu-1",
        tool_name="Bash",
        input_hash="deadbeef",
        ts=datetime(2026, 5, 22, 9, 5, tzinfo=UTC),
        ended_ts=datetime(2026, 5, 22, 9, 5, 30, tzinfo=UTC),
        is_error=True,
        error_kind="timeout",
    )


def _incident_row() -> TelemetryIncident:
    return TelemetryIncident(
        incident_id="inc-1",
        severity=IncidentSeverity.HIGH,
        cause=IncidentCause.UNKNOWN,
        ts=datetime(2026, 5, 22, 9, 10, tzinfo=UTC),
        summary="example incident",
    )


# ---------------------------------------------------------------------------
# DDL generation from the row models
# ---------------------------------------------------------------------------


def test_render_ddl_emits_one_statement_per_table() -> None:
    statements = render_ddl()
    assert len(statements) == len(TABLES)
    for spec, sql in zip(TABLES, render_ddl(), strict=True):
        assert sql.startswith(f"CREATE TABLE IF NOT EXISTS {spec.name} (")


def test_render_create_table_columns_match_model_fields() -> None:
    spec = next(s for s in TABLES if s.name == "telemetry_projects")
    sql = render_create_table(spec)
    for field_name in TelemetryProject.model_fields:
        assert field_name in sql
    # No phantom columns: every model field is the source of the column set.
    assert sql.count("\n") == len(TelemetryProject.model_fields) + 1


def test_render_create_table_single_pk_is_inline() -> None:
    spec = next(s for s in TABLES if s.name == "telemetry_sessions")
    sql = render_create_table(spec)
    assert "session_id TEXT PRIMARY KEY" in sql
    assert "session_log_path TEXT NOT NULL" in sql


def test_render_create_table_composite_pk_is_trailing_clause() -> None:
    spec = next(s for s in TABLES if s.name == "telemetry_turns")
    sql = render_create_table(spec)
    assert "PRIMARY KEY (session_id, turn_idx)" in sql


def test_column_sql_type_maps_python_types() -> None:
    assert column_sql_type(bool) == "INTEGER"
    assert column_sql_type(int) == "INTEGER"
    assert column_sql_type(int | None) == "INTEGER"
    assert column_sql_type(float) == "REAL"
    assert column_sql_type(Decimal) == "TEXT"
    assert column_sql_type(datetime) == "TEXT"
    assert column_sql_type(datetime | None) == "TEXT"
    assert column_sql_type(str) == "TEXT"


def test_column_sql_type_maps_enum_and_literal() -> None:
    assert column_sql_type(IncidentSeverity) == "TEXT"
    # ``runtime`` is a Literal string set on TelemetrySession.
    runtime_field = TelemetrySession.model_fields["runtime"]
    assert column_sql_type(runtime_field.annotation) == "TEXT"


# ---------------------------------------------------------------------------
# Round-trip (write rows, read back, assert equality)
# ---------------------------------------------------------------------------


def test_sqlite_round_trip_project(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    row = _project_row()
    store.upsert("telemetry_projects", row)
    store.commit()
    back = store.fetch_all("telemetry_projects", TelemetryProject)
    store.close()
    assert back == [row]


def test_sqlite_round_trip_session_preserves_decimal(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    row = _session_row()
    store.upsert("telemetry_sessions", row)
    store.commit()
    (back,) = store.fetch_all("telemetry_sessions", TelemetrySession)
    store.close()
    assert back == row
    assert back.total_cost_usd == Decimal("7.314159")
    assert isinstance(back.total_cost_usd, Decimal)


def test_sqlite_round_trip_tool_call_preserves_bool_and_enum(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    row = _tool_call_row()
    store.upsert("telemetry_tool_calls", row)
    store.commit()
    (back,) = store.fetch_all("telemetry_tool_calls", TelemetryToolCall)
    store.close()
    assert back == row
    assert back.is_error is True
    assert back.error_kind == "timeout"


def test_sqlite_round_trip_incident_preserves_enums(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    row = _incident_row()
    store.upsert("telemetry_incidents", row)
    store.commit()
    (back,) = store.fetch_all("telemetry_incidents", TelemetryIncident)
    store.close()
    assert back == row
    assert back.severity == IncidentSeverity.HIGH
    assert back.cause == IncidentCause.UNKNOWN


def test_upsert_replaces_on_primary_key(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    store.upsert("telemetry_projects", _project_row())
    updated = _project_row().model_copy(update={"repo_name": "renamed"})
    store.upsert("telemetry_projects", updated)
    store.commit()
    back = store.fetch_all("telemetry_projects", TelemetryProject)
    store.close()
    assert len(back) == 1
    assert back[0].repo_name == "renamed"


def test_bulk_upsert_writes_all_rows(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    rows = [
        _project_row(),
        _project_row().model_copy(update={"project_id": "def456"}),
    ]
    store.bulk_upsert("telemetry_projects", rows)
    store.commit()
    back = store.fetch_all("telemetry_projects", TelemetryProject)
    store.close()
    assert {r.project_id for r in back} == {"abc123", "def456"}


# ---------------------------------------------------------------------------
# Idempotent init_schema
# ---------------------------------------------------------------------------


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    # Second call must not raise and must not duplicate the schema-meta row.
    store.init_schema()
    meta = store.fetch_all("telemetry_schema_meta", TelemetrySchemaMeta)
    store.close()
    versions = [m for m in meta if isinstance(m, TelemetrySchemaMeta)]
    assert len(versions) == 1
    assert versions[0].key == "telemetry_schema_version"
    assert versions[0].value == SCHEMA_VERSION


def test_init_schema_preserves_existing_rows(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    store.upsert("telemetry_projects", _project_row())
    store.commit()
    store.init_schema()  # re-init must not drop data.
    back = store.fetch_all("telemetry_projects", TelemetryProject)
    store.close()
    assert back == [_project_row()]


# ---------------------------------------------------------------------------
# Factory + ImportError fallback
# ---------------------------------------------------------------------------


def test_open_store_sqlite_returns_sqlite(tmp_path: Path) -> None:
    store = open_store("sqlite", tmp_path / "m.db")
    assert isinstance(store, SqliteMetricsStore)
    assert store.backend == "sqlite"
    store.close()


def test_open_store_unknown_backend_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown store backend"):
        open_store("redis", tmp_path / "m.db")  # type: ignore[arg-type]


def test_open_store_duckdb_falls_back_to_sqlite_on_import_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "duckdb":
            raise ImportError("forced: duckdb not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    store = open_store("duckdb", tmp_path / "m.db")
    assert isinstance(store, SqliteMetricsStore)
    assert store.backend == "sqlite"
    # The fallback store is fully usable.
    store.init_schema()
    store.upsert("telemetry_projects", _project_row())
    store.commit()
    assert store.fetch_all("telemetry_projects", TelemetryProject) == [_project_row()]
    store.close()


def test_abstract_store_cannot_be_instantiated(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        AbstractMetricsStore(tmp_path / "m.db")  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# DuckDB upsert dialect — ON CONFLICT DO UPDATE, never INSERT OR REPLACE
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Minimal stand-in for a DuckDB connection that records executed SQL."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> _RecordingConn:
        self.statements.append((sql, list(params) if params is not None else []))
        return self


def _duckdb_store_with_recording_conn() -> Any:
    """Build a ``DuckDbMetricsStore`` whose connection records SQL.

    Bypasses the real ``duckdb`` driver (an optional dependency that may be
    absent) by stubbing ``_connect`` so the upsert SQL can be inspected
    without a live database.
    """
    from eawf.telemetry.store.duckdb_store import DuckDbMetricsStore

    store = DuckDbMetricsStore.__new__(DuckDbMetricsStore)
    store.path = Path("unused.db")
    store._conn = _RecordingConn()
    return store


def test_duckdb_upsert_uses_on_conflict_do_update() -> None:
    """DuckDB's upsert emits ``ON CONFLICT DO UPDATE``, not ``INSERT OR REPLACE``."""
    store = _duckdb_store_with_recording_conn()
    store.upsert("telemetry_projects", _project_row())
    sql, params = store._conn.statements[-1]
    assert "INSERT OR REPLACE" not in sql
    assert "ON CONFLICT (project_id) DO UPDATE SET" in sql
    # The primary key is the matched-on conflict target, never re-assigned.
    assert "project_id = excluded.project_id" not in sql
    # A non-key column is updated from the proposed row.
    assert "repo_name = excluded.repo_name" in sql
    assert len(params) == len(TelemetryProject.model_fields)


def test_duckdb_upsert_composite_pk_lists_all_key_columns() -> None:
    """A composite primary key lands as the full ``ON CONFLICT (...)`` target."""
    store = _duckdb_store_with_recording_conn()
    row = TelemetryCompaction(
        session_id="s1",
        ts=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
        pre_tokens=1000,
        trigger="auto",
    )
    store.upsert("telemetry_compactions", row)
    sql, _ = store._conn.statements[-1]
    assert "ON CONFLICT (session_id, ts) DO UPDATE SET" in sql
    # Both key columns are the conflict target, neither is re-assigned.
    assert "session_id = excluded.session_id" not in sql
    assert "ts = excluded.ts" not in sql
    # A non-key column is updated.
    assert "pre_tokens = excluded.pre_tokens" in sql


def test_duckdb_round_trip_when_driver_present(tmp_path: Path) -> None:
    """When ``duckdb`` is installed, the ON CONFLICT upsert actually executes."""
    pytest.importorskip("duckdb")
    from eawf.telemetry.store.duckdb_store import DuckDbMetricsStore

    store = DuckDbMetricsStore(tmp_path / "m.duckdb")
    store.init_schema()
    store.upsert("telemetry_projects", _project_row())
    store.commit()
    # A second upsert on the same PK must update in place, not error or dup.
    updated = _project_row().model_copy(update={"repo_name": "renamed"})
    store.upsert("telemetry_projects", updated)
    store.commit()
    back = store.fetch_all("telemetry_projects", TelemetryProject)
    store.close()
    assert len(back) == 1
    assert back[0].repo_name == "renamed"
