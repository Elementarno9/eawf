"""Tests for the codex_session + opencode_session telemetry source adapters (P27-W14)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity
from eawf.telemetry.models import TelemetryIncident, TelemetrySession
from eawf.telemetry.sources import (
    CodexSessionSource,
    OpenCodeSessionSource,
    SessionSource,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
_CODEX_FIXTURES = _FIXTURES / "codex"

_CODEX_OK = _CODEX_FIXTURES / "rollout-2026-05-14T00-00-00-placeholder-cccc.jsonl"
_CODEX_CORRUPT = _CODEX_FIXTURES / "rollout-2026-05-15T00-00-00-placeholder-dddd.jsonl"

# The known-good drizzle migration name the OpenCode adapter projects against.
_KNOWN_MIGRATION = "20260428004200_add_session_path"
_UNKNOWN_MIGRATION = "29991231000000_some_future_migration"


# --------------------------------------------------------------------------- #
# OpenCode SQLite fixture builders (stdlib sqlite3, PII-free placeholder ids).
# --------------------------------------------------------------------------- #


def _build_opencode_db(
    db_path: Path,
    *,
    latest_migration: str | None,
    with_session: bool = True,
) -> None:
    """Build a minimal OpenCode-shaped SQLite db at *db_path*.

    Seeds ``__drizzle_migrations`` so its latest-id row carries *latest_migration*
    (when not ``None``), plus a ``session`` + ``part`` pair holding two
    ``step-finish`` parts so the adapter can fold token totals.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE __drizzle_migrations "
            "(id INTEGER PRIMARY KEY, hash text NOT NULL, created_at numeric, "
            "name text, applied_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE session "
            "(id TEXT PRIMARY KEY, project_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, title TEXT)"
        )
        conn.execute(
            "CREATE TABLE part "
            "(id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, data TEXT)"
        )
        migrations = [
            (1, "", 1769552633000, "20260127222353_familiar_lady_ursula", "2026-05-01T00:00:00Z"),
        ]
        if latest_migration is not None:
            migrations.append((2, "", 1777336920000, latest_migration, "2026-05-01T00:00:00Z"))
        conn.executemany(
            "INSERT INTO __drizzle_migrations (id, hash, created_at, name, applied_at) "
            "VALUES (?, ?, ?, ?, ?)",
            migrations,
        )
        if with_session:
            conn.execute(
                "INSERT INTO session (id, project_id, time_created, time_updated, title) "
                "VALUES (?, ?, ?, ?, ?)",
                ("sess-placeholder-eeee", "proj-1", 1777000000000, 1777000060000, "abc work"),
            )
            step_a = {
                "type": "step-finish",
                "reason": "tool-calls",
                "tokens": {
                    "input": 1000,
                    "output": 120,
                    "reasoning": 0,
                    "cache": {"write": 50, "read": 200},
                },
                "cost": 0,
            }
            step_b = {
                "type": "step-finish",
                "reason": "stop",
                "tokens": {
                    "input": 3000,
                    "output": 400,
                    "reasoning": 0,
                    "cache": {"write": 10, "read": 1300},
                },
                "cost": 0,
            }
            text_part = {"type": "text", "text": "hello"}
            conn.executemany(
                "INSERT INTO part (id, message_id, session_id, time_created, data) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("part-1", "msg-1", "sess-placeholder-eeee", 1, json.dumps(text_part)),
                    ("part-2", "msg-1", "sess-placeholder-eeee", 2, json.dumps(step_a)),
                    ("part-3", "msg-2", "sess-placeholder-eeee", 3, json.dumps(step_b)),
                ],
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Protocol conformance.
# --------------------------------------------------------------------------- #


def test_codex_session_source_is_session_source() -> None:
    assert isinstance(CodexSessionSource(), SessionSource)


def test_opencode_session_source_is_session_source() -> None:
    assert isinstance(OpenCodeSessionSource(), SessionSource)


def test_source_names_are_stable() -> None:
    assert CodexSessionSource().source_name == "codex"
    assert OpenCodeSessionSource().source_name == "opencode"


# --------------------------------------------------------------------------- #
# CodexSessionSource.iter_rows.
# --------------------------------------------------------------------------- #


def test_codex_session_parses_into_telemetry_session() -> None:
    rows = list(CodexSessionSource().iter_rows(_CODEX_OK))
    assert len(rows) == 1
    session = rows[0]
    assert isinstance(session, TelemetrySession)
    assert session.session_id == "sess-placeholder-cccc"
    assert session.runtime == "codex"
    assert session.session_log_path == str(_CODEX_OK)


def test_codex_session_adopts_cumulative_token_totals() -> None:
    session = next(iter(CodexSessionSource().iter_rows(_CODEX_OK)))
    # Last token_count event: input=3000, cached=500, output=800, reasoning=150.
    assert session.total_input_tokens == 2500  # 3000 - 500 cached
    assert session.total_cache_read == 500
    assert session.total_output_tokens == 950  # 800 + 150 reasoning
    assert session.total_cache_write == 0


def test_codex_session_counts_turn_contexts() -> None:
    session = next(iter(CodexSessionSource().iter_rows(_CODEX_OK)))
    assert session.turn_count == 2


def test_codex_session_derives_metadata() -> None:
    session = next(iter(CodexSessionSource().iter_rows(_CODEX_OK)))
    assert session.model_primary == "gpt-5.5"
    assert session.git_branch_first == "feature/abc-widget"
    assert session.duration_ms == 120_000
    assert session.end_marker == "other"


def test_codex_session_skips_corrupt_line_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = CodexSessionSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.codex_session"):
        rows = list(source.iter_rows(_CODEX_CORRUPT))
    assert len(rows) == 1
    assert rows[0].session_id == "sess-placeholder-dddd"
    assert rows[0].total_output_tokens == 120
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "line=2" in warnings[0].message
    assert "skipped malformed json" in warnings[0].message


def test_codex_session_missing_path_yields_nothing() -> None:
    rows = list(CodexSessionSource().iter_rows(_CODEX_FIXTURES / "does-not-exist.jsonl"))
    assert rows == []


def test_codex_session_empty_file_yields_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "rollout-empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert list(CodexSessionSource().iter_rows(empty)) == []


def test_codex_session_all_corrupt_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "rollout-bad.jsonl"
    path.write_text("{bad\n[also bad\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.codex_session"):
        rows = list(CodexSessionSource().iter_rows(path))
    assert rows == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_codex_session_non_object_record_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "rollout-nonobj.jsonl"
    path.write_text('[1, 2, 3]\n"bare string"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.codex_session"):
        rows = list(CodexSessionSource().iter_rows(path))
    assert rows == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("non-object record" in r.message for r in warnings)


def test_codex_session_falls_back_to_stem_session_id(tmp_path: Path) -> None:
    path = tmp_path / "rollout-fallback-id.jsonl"
    path.write_text(
        '{"timestamp":"2026-05-14T00:00:00Z","type":"event_msg","payload":{}}\n', encoding="utf-8"
    )
    session = next(iter(CodexSessionSource().iter_rows(path)))
    assert session.session_id == "rollout-fallback-id"


def test_codex_session_no_timestamps_leaves_duration_none(tmp_path: Path) -> None:
    path = tmp_path / "rollout-no-ts.jsonl"
    path.write_text('{"type":"session_meta","payload":{"id":"sess-x"}}\n', encoding="utf-8")
    session = next(iter(CodexSessionSource().iter_rows(path)))
    assert session.started_at is None
    assert session.ended_at is None
    assert session.duration_ms is None


# --------------------------------------------------------------------------- #
# CodexSessionSource.discover.
# --------------------------------------------------------------------------- #


def test_codex_session_discover_finds_rollouts_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "2026" / "05" / "14"
    nested.mkdir(parents=True)
    (nested / "rollout-a.jsonl").write_text("{}\n", encoding="utf-8")
    (nested / "rollout-b.jsonl").write_text("{}\n", encoding="utf-8")
    (nested / "ignore.jsonl").write_text("{}\n", encoding="utf-8")
    found = [p.name for p in CodexSessionSource().discover(tmp_path)]
    assert found == ["rollout-a.jsonl", "rollout-b.jsonl"]


def test_codex_session_discover_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert list(CodexSessionSource().discover(tmp_path / "nope")) == []


# --------------------------------------------------------------------------- #
# OpenCodeSessionSource.iter_rows — known fingerprint projects.
# --------------------------------------------------------------------------- #


def test_opencode_session_projects_on_known_fingerprint(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=_KNOWN_MIGRATION)
    source = OpenCodeSessionSource()
    rows = list(source.iter_rows(db))
    assert len(rows) == 1
    session = rows[0]
    assert isinstance(session, TelemetrySession)
    assert session.session_id == "sess-placeholder-eeee"
    assert session.runtime == "opencode"
    assert source.drift_incidents == []


def test_opencode_session_aggregates_step_finish_tokens(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=_KNOWN_MIGRATION)
    session = next(iter(OpenCodeSessionSource().iter_rows(db)))
    # Two step-finish parts: input 1000+3000, output 120+400, read 200+1300, write 50+10.
    assert session.total_input_tokens == 4000
    assert session.total_output_tokens == 520
    assert session.total_cache_read == 1500
    assert session.total_cache_write == 60
    assert session.turn_count == 2  # only step-finish parts; the text part is skipped


def test_opencode_session_derives_session_timing(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=_KNOWN_MIGRATION)
    session = next(iter(OpenCodeSessionSource().iter_rows(db)))
    assert session.duration_ms == 60_000  # time_updated - time_created = 60s
    assert session.model_primary == "opencode"
    assert session.end_marker == "other"


# --------------------------------------------------------------------------- #
# OpenCodeSessionSource.iter_rows — unknown fingerprint -> Incident + skip.
# --------------------------------------------------------------------------- #


def test_opencode_session_unknown_fingerprint_emits_incident_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=_UNKNOWN_MIGRATION)
    source = OpenCodeSessionSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.opencode_session"):
        rows = list(source.iter_rows(db))
    # Projection is skipped entirely on drift.
    assert rows == []
    # A typed incident is recorded for the caller to project.
    assert len(source.drift_incidents) == 1
    incident = source.drift_incidents[0]
    assert isinstance(incident, TelemetryIncident)
    assert incident.cause is IncidentCause.EXTERNAL_API_FAILURE
    assert incident.severity is IncidentSeverity.MEDIUM
    assert _UNKNOWN_MIGRATION in incident.summary
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "unknown drizzle fingerprint" in warnings[0].message


def test_opencode_session_missing_drizzle_table_is_drift(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    source = OpenCodeSessionSource()
    rows = list(source.iter_rows(db))
    assert rows == []
    assert len(source.drift_incidents) == 1
    assert source.drift_incidents[0].cause is IncidentCause.EXTERNAL_API_FAILURE
    assert "absent" in source.drift_incidents[0].summary


def test_opencode_session_empty_drizzle_table_is_drift(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=None, with_session=False)
    # latest_migration=None still inserts the seed row, so force-empty the table.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM __drizzle_migrations")
    conn.commit()
    conn.close()
    source = OpenCodeSessionSource()
    rows = list(source.iter_rows(db))
    assert rows == []
    assert len(source.drift_incidents) == 1
    assert source.drift_incidents[0].cause is IncidentCause.EXTERNAL_API_FAILURE


def test_opencode_session_query_failure_is_skipped_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A known fingerprint but a moved column makes the relational query fail.

    The adapter must catch the resulting ``sqlite3.OperationalError``, record a
    query-failure incident, and yield nothing — never let the error escape.
    """
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE __drizzle_migrations "
            "(id INTEGER PRIMARY KEY, hash text NOT NULL, created_at numeric, "
            "name text, applied_at TEXT)"
        )
        conn.execute(
            "INSERT INTO __drizzle_migrations (id, hash, created_at, name, applied_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "", 1777336920000, _KNOWN_MIGRATION, "2026-05-01T00:00:00Z"),
        )
        # A ``session`` table missing the ``time_created`` column the query selects.
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    source = OpenCodeSessionSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.opencode_session"):
        rows = list(source.iter_rows(db))
    assert rows == []
    assert len(source.drift_incidents) == 1
    incident = source.drift_incidents[0]
    assert isinstance(incident, TelemetryIncident)
    assert incident.cause is IncidentCause.EXTERNAL_API_FAILURE
    assert incident.severity is IncidentSeverity.MEDIUM
    assert "query failed" in incident.summary
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "relational query failure" in warnings[0].message


def test_opencode_session_part_query_failure_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken ``part`` table (per-session query) is also caught gracefully."""
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE __drizzle_migrations "
            "(id INTEGER PRIMARY KEY, hash text NOT NULL, created_at numeric, "
            "name text, applied_at TEXT)"
        )
        conn.execute(
            "INSERT INTO __drizzle_migrations (id, hash, created_at, name, applied_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "", 1777336920000, _KNOWN_MIGRATION, "2026-05-01T00:00:00Z"),
        )
        conn.execute(
            "CREATE TABLE session "
            "(id TEXT PRIMARY KEY, project_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, title TEXT)"
        )
        conn.execute(
            "INSERT INTO session (id, project_id, time_created, time_updated, title) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sess-placeholder-eeee", "proj-1", 1777000000000, 1777000060000, "abc work"),
        )
        # ``part`` exists but lacks the ``data`` column the per-session query selects.
        conn.execute("CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    source = OpenCodeSessionSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.opencode_session"):
        rows = list(source.iter_rows(db))
    assert rows == []
    assert len(source.drift_incidents) == 1
    assert source.drift_incidents[0].cause is IncidentCause.EXTERNAL_API_FAILURE
    assert "query failed" in source.drift_incidents[0].summary


def test_opencode_session_missing_path_yields_nothing() -> None:
    source = OpenCodeSessionSource()
    rows = list(source.iter_rows(Path("/nonexistent/opencode.db")))
    assert rows == []
    assert source.drift_incidents == []


# --------------------------------------------------------------------------- #
# OpenCodeSessionSource.discover.
# --------------------------------------------------------------------------- #


def test_opencode_session_discover_finds_db(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _build_opencode_db(db, latest_migration=_KNOWN_MIGRATION, with_session=False)
    found = [p.name for p in OpenCodeSessionSource().discover(tmp_path)]
    assert found == ["opencode.db"]


def test_opencode_session_discover_missing_db_yields_nothing(tmp_path: Path) -> None:
    assert list(OpenCodeSessionSource().discover(tmp_path)) == []


def test_opencode_session_discover_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert list(OpenCodeSessionSource().discover(tmp_path / "nope")) == []
