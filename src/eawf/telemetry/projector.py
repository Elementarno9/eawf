"""Telemetry projector — drive sources, upsert rows into the metrics store.

The projector is the canonical rebuild entry point (C09 §5.9.4): it walks
the registered source adapters, drives each through the
:class:`~eawf.telemetry.sources.base.SessionSource` protocol, rolls the
yielded rows through the aggregator, and upserts them into an
:class:`~eawf.telemetry.store.base.AbstractMetricsStore`.

Two rebuild modes share one code path:

* :attr:`RebuildMode.FULL` re-projects every byte of every discovered
  source file. It is **idempotent**: each upsert is keyed on the store
  table's primary key (W12's ``INSERT OR REPLACE``), so a second full
  rebuild produces byte-identical row counts.
* :attr:`RebuildMode.INCREMENTAL` consults
  :class:`~eawf.telemetry.models.TelemetryFileMeta` per source file and
  projects only the bytes past the recorded ``last_offset`` — the tail
  appended since the last scan — then advances the offset to the new
  end-of-file. A file whose size is unchanged since the last scan
  contributes nothing.

The offset is a byte position into the source file, tracked per
``jsonl_path`` in the ``telemetry_file_meta`` table. Tail bytes are
sliced by the projector and handed to the same adapter parser the full
path uses, so the line-per-row stores (event / audit / report JSONL)
project only their appended suffix without re-reading the head.

Every adapter is driven one file at a time so the projector keeps a
bounded working set (C09 §5.9.4 bounded-memory invariant).
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from eawf.store.envelope import Envelope
from eawf.telemetry.aggregator import incident_from_envelope, roll_session
from eawf.telemetry.models import (
    TelemetryFileMeta,
    TelemetryIncident,
    TelemetrySession,
)
from eawf.telemetry.sources.base import SessionSource
from eawf.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)

_SESSIONS_TABLE = "telemetry_sessions"
_INCIDENTS_TABLE = "telemetry_incidents"
_FILE_META_TABLE = "telemetry_file_meta"


class RebuildMode(StrEnum):
    """Projection rebuild mode (mirrors ``eawf metrics rebuild --full|--incremental``).

    - :attr:`FULL` — re-project every byte of every source file.
    - :attr:`INCREMENTAL` — project only bytes past each file's recorded
      ``telemetry_file_meta.last_offset`` and advance the offset.
    """

    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One source adapter bound to the project root it discovers under.

    Attributes:
        source: The source adapter driven through the
            :class:`~eawf.telemetry.sources.base.SessionSource` protocol.
        root: The directory (or state path) the adapter discovers files
            under.
        project_id: The owning project's id, stamped onto session rows by
            the aggregator.
    """

    source: SessionSource[object]
    root: Path
    project_id: str


@dataclass(slots=True)
class RebuildReport:
    """Counts of rows projected by one :func:`rebuild` invocation.

    Attributes:
        sessions: Number of session rows upserted.
        incidents: Number of incident rows upserted.
        files_scanned: Number of source files read (head + tail).
        files_skipped: Number of files skipped in incremental mode
            because their size was unchanged since the last scan.
    """

    sessions: int = 0
    incidents: int = 0
    files_scanned: int = 0
    files_skipped: int = 0


@dataclass(slots=True)
class _FileMetaIndex:
    """In-memory cache of the ``telemetry_file_meta`` rows by path."""

    by_path: dict[str, TelemetryFileMeta] = field(default_factory=dict)

    @classmethod
    def load(cls, store: AbstractMetricsStore) -> _FileMetaIndex:
        """Read the persisted file-meta rows into a path-keyed index."""
        rows = store.fetch_all(_FILE_META_TABLE, TelemetryFileMeta)
        index: dict[str, TelemetryFileMeta] = {}
        for row in rows:
            assert isinstance(row, TelemetryFileMeta)
            index[row.jsonl_path] = row
        return cls(by_path=index)

    def offset_for(self, path: str) -> int:
        """Return the recorded ``last_offset`` for *path*, or ``0`` when unseen."""
        meta = self.by_path.get(path)
        return meta.last_offset if meta is not None else 0


def rebuild(
    store: AbstractMetricsStore,
    specs: Iterable[SourceSpec],
    *,
    mode: RebuildMode = RebuildMode.FULL,
) -> RebuildReport:
    """Rebuild the telemetry projection from the registered source specs.

    Drives each spec's adapter through the source protocol, rolls the
    yielded rows through the aggregator, and upserts them into *store*.
    The mode controls how much of each source file is read:

    * :attr:`RebuildMode.FULL` reads every file whole and re-projects it
      (idempotent — keyed upserts produce identical row counts on replay).
    * :attr:`RebuildMode.INCREMENTAL` reads only the bytes past each
      file's recorded ``last_offset`` and advances the offset to the new
      end-of-file.

    Args:
        store: An initialised metrics store (call ``init_schema`` first).
        specs: The source specs to project, one adapter + root each.
        mode: The rebuild mode.

    Returns:
        A :class:`RebuildReport` tallying the rows + files projected.
    """
    index = _FileMetaIndex.load(store)
    report = RebuildReport()
    for spec in specs:
        _project_spec(store, spec, mode=mode, index=index, report=report)
    store.commit()
    logger.info(
        f"rebuild mode={mode.value} sessions={report.sessions} "
        f"incidents={report.incidents} scanned={report.files_scanned} "
        f"skipped={report.files_skipped}"
    )
    return report


def _project_spec(
    store: AbstractMetricsStore,
    spec: SourceSpec,
    *,
    mode: RebuildMode,
    index: _FileMetaIndex,
    report: RebuildReport,
) -> None:
    """Project one source spec: discover its files and fold each one."""
    for path in spec.source.discover(spec.root):
        _project_file(store, spec, path, mode=mode, index=index, report=report)
    _drain_drift_incidents(store, spec.source, report)


def _project_file(
    store: AbstractMetricsStore,
    spec: SourceSpec,
    path: Path,
    *,
    mode: RebuildMode,
    index: _FileMetaIndex,
    report: RebuildReport,
) -> None:
    """Project a single discovered file, honouring the offset in incremental mode."""
    size = path.stat().st_size
    start_offset = 0 if mode is RebuildMode.FULL else index.offset_for(str(path))
    if start_offset >= size and mode is RebuildMode.INCREMENTAL:
        report.files_skipped += 1
        return

    report.files_scanned += 1
    for row in _iter_rows_from(spec.source, path, start_offset=start_offset):
        _upsert_row(store, spec, row, report)
    _stamp_file_meta(store, path, size=size, index=index)


def _iter_rows_from(
    source: SessionSource[object],
    path: Path,
    *,
    start_offset: int,
) -> Iterator[object]:
    """Yield rows from *path* starting at byte *start_offset*.

    At offset ``0`` the adapter parses the file directly. At a non-zero
    offset the tail bytes are sliced into a temporary file and parsed
    through the same adapter, so the line-per-row stores project only the
    appended suffix without re-reading the head.
    """
    if start_offset <= 0:
        yield from source.iter_rows(path)
        return
    with path.open("rb") as handle:
        handle.seek(start_offset)
        tail = handle.read()
    if not tail:
        return
    with tempfile.NamedTemporaryFile(
        prefix="eawf-telemetry-tail-", suffix=path.suffix, delete=True
    ) as tmp:
        tmp.write(tail)
        tmp.flush()
        yield from source.iter_rows(Path(tmp.name))


def _upsert_row(
    store: AbstractMetricsStore,
    spec: SourceSpec,
    row: object,
    report: RebuildReport,
) -> None:
    """Roll one source row through the aggregator and upsert it."""
    if isinstance(row, TelemetrySession):
        store.upsert(_SESSIONS_TABLE, roll_session(row, project_id=spec.project_id))
        report.sessions += 1
        return
    if isinstance(row, Envelope):
        incident = incident_from_envelope(row)
        if incident is not None:
            store.upsert(_INCIDENTS_TABLE, incident)
            report.incidents += 1
        return
    if isinstance(row, TelemetryIncident):
        store.upsert(_INCIDENTS_TABLE, row)
        report.incidents += 1
        return
    raise TypeError(f"unhandled source row type: {type(row).__name__!r}")


def _drain_drift_incidents(
    store: AbstractMetricsStore,
    source: SessionSource[object],
    report: RebuildReport,
) -> None:
    """Upsert any schema-drift incidents an adapter recorded during discovery.

    The OpenCode adapter records drift incidents on a ``drift_incidents``
    list (C09 §6 F21) rather than yielding them; the projector drains the
    list and upserts each one. Adapters without the attribute contribute
    nothing.
    """
    drift = getattr(source, "drift_incidents", None)
    if not isinstance(drift, list):
        return
    for incident in drift:
        if isinstance(incident, TelemetryIncident):
            store.upsert(_INCIDENTS_TABLE, incident)
            report.incidents += 1
    drift.clear()


def _stamp_file_meta(
    store: AbstractMetricsStore,
    path: Path,
    *,
    size: int,
    index: _FileMetaIndex,
) -> None:
    """Advance the ``telemetry_file_meta`` cursor for *path* to end-of-file."""
    stat = path.stat()
    meta = TelemetryFileMeta(
        jsonl_path=str(path),
        mtime=stat.st_mtime,
        size=size,
        last_offset=size,
        last_scan_ts=datetime.now(tz=UTC),
    )
    store.upsert(_FILE_META_TABLE, meta)
    index.by_path[meta.jsonl_path] = meta


__all__ = [
    "RebuildMode",
    "RebuildReport",
    "SourceSpec",
    "rebuild",
]
