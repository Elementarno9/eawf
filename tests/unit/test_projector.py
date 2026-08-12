"""Unit tests for the telemetry projector.

Covers the two load-bearing guarantees of the wave:

- **Idempotent ``--full``** — running a full rebuild twice yields identical
  row counts (the store upsert is keyed on the table primary key, so a
  replay overwrites in place rather than appending).
- **``--incremental`` tail scan** — an incremental rebuild projects only the
  rows whose bytes lie past the recorded
  :class:`~eawf.observability.telemetry.models.TelemetryFileMeta.last_offset`, and
  advances the offset to the new end-of-file. The fixture projects a file,
  appends rows, rebuilds incrementally, and asserts only the appended rows
  landed.

The event-incident path is driven through a small in-test source so the
offset semantics are observable on a line-per-row store; the session path
is driven through a synthetic per-runtime adapter.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import IncidentCause, IncidentSeverity, StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.models import (
    TelemetryFileMeta,
    TelemetryIncident,
    TelemetrySession,
)
from eawf.observability.telemetry.pricing import PRICING
from eawf.observability.telemetry.projector import (
    RebuildMode,
    SourceSpec,
    _iter_rows_from,
    rebuild,
)
from eawf.observability.telemetry.store import SqliteMetricsStore

_TS = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _event_line(env_id: str, event_type: str, cause: str = "RUNTIME_TIMEOUT") -> str:
    env = Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id="ABC",
        created_at=_TS,
        updated_at=None,
        summary=f"{event_type} {env_id}",
        payload={
            "event_type": event_type,
            "cause": cause,
            "timestamp": _TS.isoformat(),
        },
    )
    return env.model_dump_json() + "\n"


def _write_event_file(path: Path, env_ids: list[str]) -> None:
    path.write_text(
        "".join(_event_line(env_id, "runtime_switched") for env_id in env_ids),
        encoding="utf-8",
    )


def _append_event_lines(path: Path, env_ids: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for env_id in env_ids:
            handle.write(_event_line(env_id, "runtime_switched"))


class _EventFileSource:
    """In-test event source: discovers ``events.jsonl`` and yields envelopes.

    Mirrors the :class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.kernel.store.envelope.Envelope` rows so the projector's
    incident path and offset bookkeeping are exercised on a line-per-row
    store. It reports ``source_name = "event_jsonl"`` so the projector treats
    it as a *line-independent* source and exercises the incremental tail-slice
    path (a fold-whole-file source would instead force a full re-read).
    """

    source_name = "event_jsonl"

    def discover(self, root: Path) -> Iterator[Path]:
        path = root / "events.jsonl"
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


class _SessionSource:
    """In-test per-runtime source: yields one session per discovered file."""

    source_name = "test_sessions"

    def discover(self, root: Path) -> Iterator[Path]:
        if root.is_dir():
            yield from sorted(root.glob("*.session"))

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        if not path.is_file():
            return
        yield TelemetrySession(
            session_id=path.stem,
            project_id="",
            runtime="claude",
            wave_id=None,
            attempt_id=None,
            session_log_path=str(path),
            started_at=_TS,
            ended_at=_TS,
            duration_ms=0,
            model_primary="claude-opus",
            end_marker="other",
        )


class _PricedSessionSource:
    """In-test source yielding one priced session with known tokens + model."""

    source_name = "test_priced_sessions"

    def discover(self, root: Path) -> Iterator[Path]:
        if root.is_dir():
            yield from sorted(root.glob("*.session"))

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        if not path.is_file():
            return
        yield TelemetrySession(
            session_id=path.stem,
            project_id="",
            runtime="claude",
            wave_id=None,
            attempt_id=None,
            session_log_path=str(path),
            started_at=_TS,
            ended_at=_TS,
            duration_ms=0,
            model_primary="claude-opus-4-7",
            total_input_tokens=1000,
            total_output_tokens=500,
            total_cache_read=2000,
            total_cache_write=800,
            end_marker="other",
        )


class _FoldWholeFileSource:
    """In-test fold-whole-file source modelling claude/codex/opencode.

    Folds the WHOLE discovered file into exactly one
    :class:`~eawf.observability.telemetry.models.TelemetrySession` row whose ``turn_count``
    equals the number of non-blank lines and whose ``session_id`` is the file
    stem. This is the shape that makes tail-slicing unsafe: a partial tail
    re-fold would yield a row with a *smaller* turn_count keyed on the same
    session id, overwriting the complete row.

    It reports a non-allowlisted ``source_name`` so the projector classifies
    it as fold-whole-file and forces a full re-read in incremental mode.
    """

    source_name = "fold_whole_file"

    def discover(self, root: Path) -> Iterator[Path]:
        if root.is_dir():
            yield from sorted(root.glob("*.session"))

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        if not path.is_file():
            return
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return
        yield TelemetrySession(
            session_id=path.stem,
            project_id="",
            runtime="claude",
            wave_id=None,
            attempt_id=None,
            session_log_path=str(path),
            started_at=_TS,
            ended_at=_TS,
            duration_ms=0,
            model_primary="claude-opus",
            turn_count=len(lines),
            end_marker="other",
        )


class _BadRowSource:
    """In-test source yielding two good incidents around one bad row.

    The bad row is an unhandled type, so the projector's per-row upsert raises
    and the row is logged + skipped — the two good incident rows around it must
    still commit.
    """

    source_name = "bad_row_source"

    def discover(self, root: Path) -> Iterator[Path]:
        if root.is_dir():
            yield from sorted(root.glob("*.bad"))

    def iter_rows(self, path: Path) -> Iterator[object]:
        if not path.is_file():
            return
        yield _incident_row("GOOD-1")
        yield object()  # unhandled row type -> _upsert_row raises TypeError
        yield _incident_row("GOOD-2")


def _incident_row(incident_id: str) -> TelemetryIncident:
    return TelemetryIncident(
        incident_id=incident_id,
        severity=IncidentSeverity.LOW,
        cause=IncidentCause.RUNTIME_TIMEOUT,
        ts=_TS,
        summary=f"incident {incident_id}",
    )


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")


def _append_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for ln in lines:
            handle.write(f"{ln}\n")


def _open_store(tmp_path: Path) -> SqliteMetricsStore:
    store = SqliteMetricsStore(tmp_path / "m.db")
    store.init_schema()
    return store


def _incidents(store: SqliteMetricsStore) -> list[TelemetryIncident]:
    rows = store.fetch_all("telemetry_incidents", TelemetryIncident)
    return [r for r in rows if isinstance(r, TelemetryIncident)]


def _sessions(store: SqliteMetricsStore) -> list[TelemetrySession]:
    rows = store.fetch_all("telemetry_sessions", TelemetrySession)
    return [r for r in rows if isinstance(r, TelemetrySession)]


def _incident_count(store: SqliteMetricsStore) -> int:
    return len(_incidents(store))


def _file_meta(store: SqliteMetricsStore, path: Path) -> TelemetryFileMeta:
    rows = store.fetch_all("telemetry_file_meta", TelemetryFileMeta)
    metas = [r for r in rows if isinstance(r, TelemetryFileMeta)]
    (meta,) = [m for m in metas if m.jsonl_path == str(path)]
    return meta


# --------------------------------------------------------------------------- #
# Idempotent --full.
# --------------------------------------------------------------------------- #


def test_full_rebuild_projects_all_incidents(tmp_path: Path) -> None:
    _write_event_file(tmp_path / "events.jsonl", ["EV-1", "EV-2", "EV-3"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    report = rebuild(store, [spec], mode=RebuildMode.FULL)

    assert report.incidents == 3
    assert _incident_count(store) == 3
    store.close()


def test_full_rebuild_twice_is_idempotent(tmp_path: Path) -> None:
    _write_event_file(tmp_path / "events.jsonl", ["EV-1", "EV-2", "EV-3"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.FULL)
    count_after_first = _incident_count(store)
    second = rebuild(store, [spec], mode=RebuildMode.FULL)
    count_after_second = _incident_count(store)

    # Replaying the full rebuild re-projects the same keyed rows: the count
    # is identical (INSERT OR REPLACE overwrites in place, never appends).
    assert second.incidents == 3
    assert count_after_first == count_after_second == 3
    store.close()


def test_full_rebuild_projects_sessions(tmp_path: Path) -> None:
    (tmp_path / "a.session").write_text("x", encoding="utf-8")
    (tmp_path / "b.session").write_text("x", encoding="utf-8")
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_SessionSource(), root=tmp_path, project_id="proj-9")

    report = rebuild(store, [spec], mode=RebuildMode.FULL)

    assert report.sessions == 2
    assert {s.project_id for s in _sessions(store)} == {"proj-9"}
    store.close()


def test_full_rebuild_prices_session_cost(tmp_path: Path) -> None:
    # End-to-end: a projected session with known tokens + a snapshot model
    # lands a non-zero total_cost_usd in the store (cost is no longer inert).
    (tmp_path / "priced.session").write_text("x", encoding="utf-8")
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_PricedSessionSource(), root=tmp_path, project_id="proj-9")

    rebuild(store, [spec], mode=RebuildMode.FULL)

    (session,) = _sessions(store)
    row = PRICING["claude-opus-4-7"]
    expected = (
        Decimal(1000) * row.input_per_token
        + Decimal(500) * row.output_per_token
        + Decimal(2000) * row.cache_read_per_token
        + Decimal(800) * row.cache_write_5m_per_token
    )
    assert session.total_cost_usd == expected
    assert session.total_cost_usd > Decimal("0")
    store.close()


def test_full_rebuild_advances_offset_to_eof(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.FULL)

    meta = _file_meta(store, path)
    assert meta.last_offset == path.stat().st_size
    store.close()


# --------------------------------------------------------------------------- #
# --incremental tail scan.
# --------------------------------------------------------------------------- #


def test_incremental_projects_only_new_rows(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    first = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    assert first.incidents == 2
    assert _incident_count(store) == 2

    # Append two new rows past the recorded offset.
    _append_event_lines(path, ["EV-3", "EV-4"])

    second = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    # Only the two appended rows are projected this pass.
    assert second.incidents == 2
    assert second.files_scanned == 1
    # Total incident rows now four (two original + two appended).
    assert _incident_count(store) == 4
    ids = {i.incident_id for i in _incidents(store)}
    assert ids == {"EV-1", "EV-2", "EV-3", "EV-4"}
    store.close()


def test_incremental_advances_offset_each_pass(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    offset_one = _file_meta(store, path).last_offset
    assert offset_one == path.stat().st_size

    _append_event_lines(path, ["EV-2"])
    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    offset_two = _file_meta(store, path).last_offset
    assert offset_two == path.stat().st_size
    assert offset_two > offset_one
    store.close()


def test_incremental_skips_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    # No new bytes appended; the second pass scans nothing.
    second = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    assert second.incidents == 0
    assert second.files_scanned == 0
    assert second.files_skipped == 1
    assert _incident_count(store) == 2
    store.close()


def test_incremental_first_pass_reads_whole_file(tmp_path: Path) -> None:
    # A never-seen file has offset 0, so the first incremental pass reads
    # the whole file (no tail-slicing).
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2", "EV-3"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    report = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    assert report.incidents == 3
    assert _incident_count(store) == 3
    store.close()


def test_incremental_tail_does_not_reproject_head(tmp_path: Path) -> None:
    # After the first pass projects EV-1, mutating EV-1's row in the store
    # and then appending EV-2 + incremental must NOT touch EV-1's row
    # (the head is past the offset and is not re-read).
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    # Mutate the projected EV-1 row in place.
    (ev1,) = _incidents(store)
    store.upsert(
        "telemetry_incidents",
        ev1.model_copy(update={"summary": "manually-edited"}),
    )
    store.commit()

    _append_event_lines(path, ["EV-2"])
    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    incidents = {i.incident_id: i for i in _incidents(store)}
    # EV-1 was not re-read, so the manual edit survives.
    assert incidents["EV-1"].summary == "manually-edited"
    assert incidents["EV-2"].summary.startswith("runtime_switched EV-2")
    store.close()


def test_full_reproject_overwrites_incremental_edits(tmp_path: Path) -> None:
    # A FULL rebuild re-reads from offset 0, so it overwrites any manual
    # edit to a head row (proving --full ignores the offset).
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    (ev1,) = _incidents(store)
    store.upsert("telemetry_incidents", ev1.model_copy(update={"summary": "edited"}))
    store.commit()

    rebuild(store, [spec], mode=RebuildMode.FULL)
    (back,) = _incidents(store)
    assert back.summary.startswith("runtime_switched EV-1")
    store.close()


# --------------------------------------------------------------------------- #
# Rotation / truncation detection.
# --------------------------------------------------------------------------- #


def test_incremental_reprojects_rotated_source_from_zero(tmp_path: Path) -> None:
    # A source that shrinks below the recorded last_offset (rotated / truncated
    # log) must be re-projected from offset 0 rather than skipped forever.
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2", "EV-3"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    big_offset = _file_meta(store, path).last_offset
    assert _incident_count(store) == 3

    # Rotate: replace the file with a smaller fresh one (size now below the
    # recorded cursor). The single new row must be picked up from offset 0.
    _write_event_file(path, ["EV-9"])
    assert path.stat().st_size < big_offset

    report = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    # The fresh row was re-projected from the start (scanned, not skipped).
    assert report.files_scanned == 1
    assert report.files_skipped == 0
    assert report.incidents == 1
    ids = {i.incident_id for i in _incidents(store)}
    assert "EV-9" in ids
    # Cursor reset then advanced to the new (smaller) end-of-file.
    assert _file_meta(store, path).last_offset == path.stat().st_size
    store.close()


def test_incremental_reprojects_truncated_to_empty_then_regrown(tmp_path: Path) -> None:
    # Truncating to empty and re-growing must re-project the new content from
    # offset 0 (the recorded cursor pointed past the truncated end-of-file).
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    assert _incident_count(store) == 2

    # Truncate to a single fresh row (smaller than the recorded cursor).
    _write_event_file(path, ["EV-7"])
    report = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    assert report.incidents == 1
    assert report.files_scanned == 1
    assert "EV-7" in {i.incident_id for i in _incidents(store)}
    store.close()


def test_incremental_reprojects_same_size_refill_via_mtime(tmp_path: Path) -> None:
    # A source that rotates / truncates and then refills to the EXACT previous
    # byte count must NOT be treated as unchanged: the size-only skip gate would
    # drop the fresh content, so the mtime guard forces a re-scan. The two
    # equal-length env-ids below produce byte-identical file sizes; only the
    # mtime distinguishes the old projection from the refilled content.
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    size_before = path.stat().st_size
    recorded_mtime = _file_meta(store, path).mtime
    assert _incident_count(store) == 2

    # Refill with two NEW rows of the same total length, then bump the mtime
    # past the recorded one (the size is unchanged on purpose).
    _write_event_file(path, ["EV-7", "EV-8"])
    assert path.stat().st_size == size_before
    os.utime(path, (recorded_mtime + 10.0, recorded_mtime + 10.0))

    report = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    # The mtime mismatch forced a re-scan (not a skip), so the fresh rows landed.
    assert report.files_scanned == 1
    assert report.files_skipped == 0
    ids = {i.incident_id for i in _incidents(store)}
    assert {"EV-7", "EV-8"} <= ids
    store.close()


def test_incremental_skips_when_size_and_mtime_both_unchanged(tmp_path: Path) -> None:
    # The dual of the refill test: when BOTH size and mtime are unchanged the
    # fast-path skip still fires (the mtime guard does not over-trigger).
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1", "EV-2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    recorded_mtime = _file_meta(store, path).mtime
    # Pin the on-disk mtime to exactly the recorded value so the gate compares
    # equal even if the first scan's stat rounded differently.
    os.utime(path, (recorded_mtime, recorded_mtime))

    second = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    assert second.files_scanned == 0
    assert second.files_skipped == 1
    assert second.incidents == 0
    assert _incident_count(store) == 2
    store.close()


# --------------------------------------------------------------------------- #
# Tail-slice safety for fold-whole-file adapters.
# --------------------------------------------------------------------------- #


def test_incremental_fold_whole_file_keeps_session_row_complete(tmp_path: Path) -> None:
    # A fold-whole-file adapter (claude/codex/opencode) folds the WHOLE file
    # into one session row. On an incremental re-run after the file grows, the
    # projector must force a FULL re-read so the session row reflects every
    # line — NOT a partial tail that would truncate the row.
    src = _FoldWholeFileSource()
    path = tmp_path / "s1.session"
    _write_lines(path, ["t0", "t1", "t2"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=src, root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    (row,) = _sessions(store)
    assert row.turn_count == 3  # folded from all three lines

    # Append one line. A naive tail-slice would re-fold only "t3" and replace
    # the complete row with turn_count == 1; the full re-read keeps it == 4.
    _append_lines(path, ["t3"])
    report = rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    (row,) = _sessions(store)
    assert row.turn_count == 4
    assert report.files_scanned == 1
    store.close()


def test_incremental_line_independent_source_still_tail_slices(tmp_path: Path) -> None:
    # The dual of the fold-whole-file guard: a line-independent source
    # (event_jsonl) must still tail-slice — a head row mutated in the store is
    # NOT re-read on the incremental pass that picks up only the appended tail.
    path = tmp_path / "events.jsonl"
    _write_event_file(path, ["EV-1"])
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_EventFileSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)
    (ev1,) = _incidents(store)
    store.upsert("telemetry_incidents", ev1.model_copy(update={"summary": "head-untouched"}))
    store.commit()

    _append_event_lines(path, ["EV-2"])
    rebuild(store, [spec], mode=RebuildMode.INCREMENTAL)

    incidents = {i.incident_id: i for i in _incidents(store)}
    # The head row was past the offset and not re-read: the edit survives.
    assert incidents["EV-1"].summary == "head-untouched"
    assert "EV-2" in incidents
    store.close()


def test_tail_slice_temp_file_is_closed_before_source_reopens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows can reopen the tail temp file because projector closes it first."""

    path = tmp_path / "events.jsonl"
    path.write_bytes(b"head\nTAIL\n")
    tmp_objects: list[Any] = []

    class _FakeNamedTemporaryFile:
        def __init__(self, *, suffix: str, delete: bool, **_kwargs: object) -> None:
            self.name = str(tmp_path / f"tail{suffix}")
            self.closed = False
            self._buffer = bytearray()
            self._delete = delete
            tmp_objects.append(self)

        def __enter__(self) -> _FakeNamedTemporaryFile:
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

        def write(self, data: bytes) -> int:
            self._buffer.extend(data)
            return len(data)

        def flush(self) -> None:
            Path(self.name).write_bytes(bytes(self._buffer))

        def close(self) -> None:
            if not self.closed:
                Path(self.name).write_bytes(bytes(self._buffer))
                self.closed = True
                if self._delete:
                    Path(self.name).unlink()

    class _TailSource:
        source_name = "event_jsonl"

        def iter_rows(self, temp_path: Path) -> Iterator[str]:
            (tmp,) = tmp_objects
            assert tmp.closed is True
            assert temp_path.exists()
            yield temp_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "eawf.observability.telemetry.projector.tempfile.NamedTemporaryFile",
        _FakeNamedTemporaryFile,
    )

    rows = list(_iter_rows_from(_TailSource(), path, start_offset=len(b"head\n")))

    assert rows == ["TAIL\n"]
    assert tmp_objects[0].closed is True
    assert not Path(tmp_objects[0].name).exists()


# --------------------------------------------------------------------------- #
# Per-row skip on a bad upsert.
# --------------------------------------------------------------------------- #


def test_rebuild_skips_bad_row_and_commits_good_rows(tmp_path: Path) -> None:
    # One row that fails to upsert (an unhandled row type) must be logged and
    # skipped without discarding the rest of the rebuild: the good rows around
    # it still commit.
    (tmp_path / "x.bad").write_text("x", encoding="utf-8")
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_BadRowSource(), root=tmp_path, project_id="p1")

    report = rebuild(store, [spec], mode=RebuildMode.FULL)

    # The good incident rows committed despite the bad row in the middle.
    ids = {i.incident_id for i in _incidents(store)}
    assert ids == {"GOOD-1", "GOOD-2"}
    assert report.incidents == 2
    assert report.rows_skipped == 1
    store.close()


def test_rebuild_bad_row_skip_persists_across_reopen(tmp_path: Path) -> None:
    # The good rows are committed (durable), not merely buffered: re-opening
    # the store sees them after the rebuild that skipped a bad row.
    (tmp_path / "x.bad").write_text("x", encoding="utf-8")
    store = _open_store(tmp_path)
    spec = SourceSpec(source=_BadRowSource(), root=tmp_path, project_id="p1")

    rebuild(store, [spec], mode=RebuildMode.FULL)
    store.close()

    reopened = SqliteMetricsStore(tmp_path / "m.db")
    ids = {i.incident_id for i in _incidents(reopened)}
    assert ids == {"GOOD-1", "GOOD-2"}
    reopened.close()
