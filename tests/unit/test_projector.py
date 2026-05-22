"""Unit tests for the telemetry projector (P27-I01-W15).

Covers the two load-bearing guarantees of the wave:

- **Idempotent ``--full``** — running a full rebuild twice yields identical
  row counts (the store upsert is keyed on the table primary key, so a
  replay overwrites in place rather than appending).
- **``--incremental`` tail scan** — an incremental rebuild projects only the
  rows whose bytes lie past the recorded
  :class:`~eawf.telemetry.models.TelemetryFileMeta.last_offset`, and
  advances the offset to the new end-of-file. The fixture projects a file,
  appends rows, rebuilds incrementally, and asserts only the appended rows
  landed.

The event-incident path is driven through a small in-test source so the
offset semantics are observable on a line-per-row store; the session path
is driven through a synthetic per-runtime adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope
from eawf.telemetry.models import (
    TelemetryFileMeta,
    TelemetryIncident,
    TelemetrySession,
)
from eawf.telemetry.projector import (
    RebuildMode,
    SourceSpec,
    rebuild,
)
from eawf.telemetry.store import SqliteMetricsStore

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

    Mirrors the :class:`~eawf.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.store.envelope.Envelope` rows so the projector's
    incident path and offset bookkeeping are exercised on a line-per-row
    store.
    """

    source_name = "test_events"

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
