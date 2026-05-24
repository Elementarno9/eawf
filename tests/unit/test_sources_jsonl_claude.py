"""Tests for the event_jsonl + claude_session telemetry source adapters (P27-W13)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from eawf.kernel.store.envelope import Envelope
from eawf.telemetry.models import TelemetrySession
from eawf.telemetry.sources import ClaudeSessionSource, EventJsonlSource, SessionSource
from eawf.telemetry.sources.base import SessionSource as SessionSourceBase

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
_STORE_FIXTURES = _FIXTURES / "store"
_CLAUDE_FIXTURES = _FIXTURES / "claude"


# --------------------------------------------------------------------------- #
# Protocol conformance.
# --------------------------------------------------------------------------- #


def test_event_jsonl_source_is_session_source() -> None:
    assert isinstance(EventJsonlSource(), SessionSource)


def test_claude_session_source_is_session_source() -> None:
    assert isinstance(ClaudeSessionSource(), SessionSource)


def test_session_source_reexported_from_package_matches_base() -> None:
    assert SessionSource is SessionSourceBase


def test_plain_object_is_not_session_source() -> None:
    assert not isinstance(object(), SessionSource)


def test_source_names_are_stable() -> None:
    assert EventJsonlSource().source_name == "event_jsonl"
    assert ClaudeSessionSource().source_name == "claude"


# --------------------------------------------------------------------------- #
# EventJsonlSource.iter_rows.
# --------------------------------------------------------------------------- #


def test_event_jsonl_yields_envelopes_from_event_store() -> None:
    rows = list(EventJsonlSource().iter_rows(_STORE_FIXTURES / "event.jsonl"))
    assert [r.id for r in rows] == [
        "EV-fixture-0001",
        "EV-fixture-0002",
        "EV-fixture-0003",
    ]
    assert all(isinstance(r, Envelope) for r in rows)


def test_event_jsonl_skips_corrupt_line_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = EventJsonlSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.event_jsonl"):
        rows = list(source.iter_rows(_STORE_FIXTURES / "event.jsonl"))
    # The corrupt mid-file line is skipped; the valid rows around it survive.
    assert len(rows) == 3
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "line=3" in warnings[0].message
    assert "skipped corrupt envelope" in warnings[0].message


def test_event_jsonl_yields_audit_envelopes() -> None:
    rows = list(EventJsonlSource().iter_rows(_STORE_FIXTURES / "audit.jsonl"))
    assert [r.id for r in rows] == ["AU-fixture-0001", "AU-fixture-0002"]
    assert all(r.kind.value == "audit" for r in rows)


def test_event_jsonl_yields_role_report_envelopes() -> None:
    rows = list(EventJsonlSource().iter_rows(_STORE_FIXTURES / "executor_report.jsonl"))
    assert len(rows) == 1
    assert rows[0].kind.value == "executor_report"
    assert rows[0].payload["body"]["wave_id"] == "W01"


def test_event_jsonl_missing_path_yields_nothing() -> None:
    rows = list(EventJsonlSource().iter_rows(_STORE_FIXTURES / "does-not-exist.jsonl"))
    assert rows == []


def test_event_jsonl_empty_file_yields_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "event.jsonl"
    empty.write_text("", encoding="utf-8")
    assert list(EventJsonlSource().iter_rows(empty)) == []


def test_event_jsonl_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "event.jsonl"
    valid = (_STORE_FIXTURES / "event.jsonl").read_text(encoding="utf-8").splitlines()[0]
    path.write_text(f"\n{valid}\n\n", encoding="utf-8")
    rows = list(EventJsonlSource().iter_rows(path))
    assert [r.id for r in rows] == ["EV-fixture-0001"]


def test_event_jsonl_all_corrupt_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "event.jsonl"
    path.write_text("{not json\n}\n{still not}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.event_jsonl"):
        rows = list(EventJsonlSource().iter_rows(path))
    assert rows == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3


# --------------------------------------------------------------------------- #
# EventJsonlSource.discover.
# --------------------------------------------------------------------------- #


def test_event_jsonl_discover_finds_existing_stores() -> None:
    state_path = _FIXTURES / "state.json"
    found = {p.name for p in EventJsonlSource().discover(state_path)}
    assert found == {"event.jsonl", "audit.jsonl", "executor_report.jsonl"}


def test_event_jsonl_discover_missing_store_dir_yields_nothing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    assert list(EventJsonlSource().discover(state_path)) == []


# --------------------------------------------------------------------------- #
# ClaudeSessionSource.iter_rows.
# --------------------------------------------------------------------------- #


def test_claude_session_parses_into_telemetry_session() -> None:
    rows = list(ClaudeSessionSource().iter_rows(_CLAUDE_FIXTURES / "sess-placeholder-aaaa.jsonl"))
    assert len(rows) == 1
    session = rows[0]
    assert isinstance(session, TelemetrySession)
    assert session.session_id == "sess-placeholder-aaaa"
    assert session.runtime == "claude"


def test_claude_session_sums_token_usage() -> None:
    session = next(
        iter(ClaudeSessionSource().iter_rows(_CLAUDE_FIXTURES / "sess-placeholder-aaaa.jsonl"))
    )
    assert session.total_input_tokens == 2400
    assert session.total_output_tokens == 630
    assert session.total_cache_read == 17100
    assert session.total_cache_write == 1500
    assert session.turn_count == 2


def test_claude_session_derives_metadata() -> None:
    session = next(
        iter(ClaudeSessionSource().iter_rows(_CLAUDE_FIXTURES / "sess-placeholder-aaaa.jsonl"))
    )
    assert session.model_primary == "claude-opus-4-7"
    assert session.git_branch_first == "feature/abc-widget"
    assert session.duration_ms == 120_000
    assert session.end_marker == "other"


def test_claude_session_computes_orphan_rate() -> None:
    session = next(
        iter(ClaudeSessionSource().iter_rows(_CLAUDE_FIXTURES / "sess-placeholder-aaaa.jsonl"))
    )
    # Three child records carry a parentUuid; one (uuid-9999) is an orphan.
    assert session.parent_uuid_orphan_rate == pytest.approx(1 / 3)


def test_claude_session_skips_corrupt_line_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = ClaudeSessionSource()
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.claude_session"):
        rows = list(source.iter_rows(_CLAUDE_FIXTURES / "sess-placeholder-bbbb.jsonl"))
    assert len(rows) == 1
    assert rows[0].total_output_tokens == 120
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "line=2" in warnings[0].message
    assert "skipped malformed json" in warnings[0].message


def test_claude_session_missing_path_yields_nothing() -> None:
    rows = list(ClaudeSessionSource().iter_rows(_CLAUDE_FIXTURES / "does-not-exist.jsonl"))
    assert rows == []


def test_claude_session_empty_file_yields_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "sess.jsonl"
    empty.write_text("", encoding="utf-8")
    assert list(ClaudeSessionSource().iter_rows(empty)) == []


def test_claude_session_all_corrupt_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sess.jsonl"
    path.write_text("{bad\n[also bad\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.claude_session"):
        rows = list(ClaudeSessionSource().iter_rows(path))
    assert rows == []
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_claude_session_non_object_record_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sess.jsonl"
    path.write_text('[1, 2, 3]\n"a bare string"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eawf.telemetry.sources.claude_session"):
        rows = list(ClaudeSessionSource().iter_rows(path))
    assert rows == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("non-object record" in r.message for r in warnings)


def test_claude_session_falls_back_to_stem_session_id(tmp_path: Path) -> None:
    path = tmp_path / "fallback-id.jsonl"
    path.write_text('{"type":"user","timestamp":"2026-05-14T00:00:00Z"}\n', encoding="utf-8")
    session = next(iter(ClaudeSessionSource().iter_rows(path)))
    assert session.session_id == "fallback-id"
    assert session.session_log_path == str(path)


def test_claude_session_no_timestamps_leaves_duration_none(tmp_path: Path) -> None:
    path = tmp_path / "no-ts.jsonl"
    path.write_text('{"type":"user","sessionId":"sess-x"}\n', encoding="utf-8")
    session = next(iter(ClaudeSessionSource().iter_rows(path)))
    assert session.started_at is None
    assert session.ended_at is None
    assert session.duration_ms is None


# --------------------------------------------------------------------------- #
# ClaudeSessionSource.discover.
# --------------------------------------------------------------------------- #


def test_claude_session_discover_finds_transcripts_sorted() -> None:
    found = [p.name for p in ClaudeSessionSource().discover(_CLAUDE_FIXTURES)]
    assert found == ["sess-placeholder-aaaa.jsonl", "sess-placeholder-bbbb.jsonl"]


def test_claude_session_discover_missing_root_yields_nothing(tmp_path: Path) -> None:
    assert list(ClaudeSessionSource().discover(tmp_path / "nope")) == []
