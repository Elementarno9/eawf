"""Telemetry projector — drive sources, upsert rows into the metrics store.

The projector is the canonical rebuild entry point (C09 §5.9.4): it walks
the registered source adapters, drives each through the
:class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol, rolls the
yielded rows through the aggregator, and upserts them into an
:class:`~eawf.observability.telemetry.store.base.AbstractMetricsStore`.

Two rebuild modes share one code path:

* :attr:`RebuildMode.FULL` re-projects every byte of every discovered
  source file. It is **idempotent**: each upsert is keyed on the store
  table's primary key (W12's ``INSERT OR REPLACE``), so a second full
  rebuild produces byte-identical row counts.
* :attr:`RebuildMode.INCREMENTAL` consults
  :class:`~eawf.observability.telemetry.models.TelemetryFileMeta` per source file and
  projects only the bytes past the recorded ``last_offset`` — the tail
  appended since the last scan — then advances the offset to the new
  end-of-file. A file whose size AND mtime are both unchanged since the
  last scan contributes nothing; a source that rotates / truncates and
  then refills to the exact previous byte count is caught by the mtime
  guard and re-scanned rather than skipped.

The offset is a byte position into the source file, tracked per
``jsonl_path`` in the ``telemetry_file_meta`` table. Tail bytes are
sliced by the projector and handed to the same adapter parser the full
path uses, so the line-per-row stores (event / audit / report JSONL)
project only their appended suffix without re-reading the head.

Incremental projection guards three failure modes:

* **Rotation / truncation** — when a source file has shrunk below its
  recorded ``last_offset`` (a rotated or truncated log), the cursor is
  reset to ``0`` and the fresh content is re-projected from the start,
  rather than the file being skipped forever.
* **Tail-slice safety** — only *line-independent* sources (every line a
  complete row, e.g. ``event_jsonl``) are tail-sliced. The per-runtime
  session adapters (``claude`` / ``codex`` / ``opencode``) fold the whole
  file into one row, so they always force a full re-read; a partial tail
  would re-fold a fragment and overwrite the complete session row with a
  truncated one.
* **Per-row isolation** — one row whose upsert fails is logged + skipped
  (counted in ``RebuildReport.rows_skipped``); the good rows already
  accumulated still commit, so a single bad row never discards the
  rebuild. The one exception is a session payload whose schema version
  this build cannot project: that is a property of the whole log, so it
  aborts the rebuild rather than reporting success over an empty session
  table.

Every adapter is driven one file at a time so the projector keeps a
bounded working set (C09 §5.9.4 bounded-memory invariant).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Final

from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.aggregator import (
    SessionPayloadSchemaError,
    incident_from_envelope,
    roll_session,
    session_from_envelope,
)
from eawf.observability.telemetry.models import (
    HEAD_DIGEST_BYTES,
    HistoryIndexEntry,
    ObservedDuration,
    ObservedRequest,
    ObservedSession,
    PriceSourceKind,
    RequestDropReason,
    SweepDropReason,
    TelemetryDispatchCost,
    TelemetryFileMeta,
    TelemetryIncident,
    TelemetrySchemaMeta,
    TelemetrySession,
)
from eawf.observability.telemetry.pricing import PriceBook, active_price_book
from eawf.observability.telemetry.sources.base import SessionSource
from eawf.observability.telemetry.sources.session_history import (
    HistoryFile,
    ParsedTranscript,
    RequestLedger,
    SessionDetail,
    TranscriptFold,
    claude_projects_root,
    codex_sessions_root,
    discover_claude_project,
    discover_history_files,
    fold_history_lines,
    history_path_digest,
    observed_rows,
    read_session_detail,
    reprice_request,
)
from eawf.observability.telemetry.store.base import AbstractMetricsStore
from eawf.runtime.lock import portalock
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

_SESSIONS_TABLE = "telemetry_sessions"
_INCIDENTS_TABLE = "telemetry_incidents"
_DISPATCH_COSTS_TABLE = "telemetry_dispatch_costs"
_FILE_META_TABLE = "telemetry_file_meta"
_OBSERVED_SESSIONS_TABLE = "telemetry_observed_sessions"
_OBSERVED_DURATIONS_TABLE = "telemetry_observed_durations"
_OBSERVED_REQUESTS_TABLE = "telemetry_observed_requests"
_HISTORY_INDEX_TABLE = "telemetry_history_index"
_SCHEMA_META_TABLE = "telemetry_schema_meta"
#: The schema-meta key holding the digest of the prices the stored costs were priced with.
_PRICE_DIGEST_KEY = "price_file_digest"

#: Source adapters whose files are line-independent: every line parses to a
#: complete, self-contained row, so the incremental tail slice (bytes past the
#: recorded offset) can be re-parsed in isolation without corrupting any row.
#: Adapters absent from this set fold the *whole* file into one row (the
#: per-runtime session adapters: ``claude`` / ``codex`` / ``opencode``) and so
#: MUST be re-read whole — a partial tail would re-fold a fragment and
#: ``INSERT OR REPLACE`` the complete session row with a truncated one. The set
#: is a positive allowlist so an unknown future adapter defaults to the safe
#: (full re-read) path rather than risking row truncation.
_LINE_INDEPENDENT_SOURCES: frozenset[str] = frozenset({"event_jsonl", "dispatch_cost"})


def _is_line_independent(source: SessionSource[object]) -> bool:
    """Return whether *source* yields one self-contained row per line.

    Line-independent sources are the only ones the projector may tail-slice in
    incremental mode; fold-whole-file adapters force a full re-read so a
    partial tail never overwrites a complete row.
    """
    return source.source_name in _LINE_INDEPENDENT_SOURCES


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
            :class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol.
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
        dispatch_costs: Number of ``dispatch_cost`` EU-forward rows upserted.
        files_scanned: Number of source files read (head + tail).
        files_skipped: Number of files skipped in incremental mode
            because their size was unchanged since the last scan.
        rows_skipped: Number of individual rows that failed to upsert and
            were logged + skipped (the good rows still committed).
    """

    sessions: int = 0
    incidents: int = 0
    dispatch_costs: int = 0
    files_scanned: int = 0
    files_skipped: int = 0
    rows_skipped: int = 0


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

    def mtime_for(self, path: str) -> float | None:
        """Return the recorded ``mtime`` for *path*, or ``None`` when unseen."""
        meta = self.by_path.get(path)
        return meta.mtime if meta is not None else None


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
        f"incidents={report.incidents} dispatch_costs={report.dispatch_costs} "
        f"scanned={report.files_scanned} skipped={report.files_skipped} "
        f"rows_skipped={report.rows_skipped}"
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
    """Project a single discovered file, honouring the offset in incremental mode.

    Three incremental-mode subtleties are handled here:

    * **Rotation / truncation** — when the file has shrunk below the recorded
      ``last_offset`` (a rotated or truncated source), the cursor is reset to
      ``0`` so the fresh content is re-projected from the start instead of
      being skipped forever.
    * **Unchanged file** — when both the file size matches the recorded cursor
      AND the mtime matches the recorded mtime, nothing has changed since the
      last scan, so it is skipped (the bounded-rebuild fast path). A
      size-only check would miss a source that rotates / truncates and then
      refills to the exact previous byte count — the mtime guard forces a
      re-scan of that content rather than dropping it.
    * **Fold-whole-file adapters** — only line-independent sources may
      tail-slice; fold-whole-file adapters force a full re-read so a partial
      tail never truncates the session row keyed on the whole file.
    """
    stat = path.stat()
    size = stat.st_size
    if (
        mode is RebuildMode.INCREMENTAL
        and index.offset_for(str(path)) == size
        and index.mtime_for(str(path)) == stat.st_mtime
    ):
        # An unchanged file (cursor at end-of-file AND mtime unchanged)
        # contributes nothing on a re-scan. A size or mtime mismatch falls
        # through: a shrunk file hits the rotation reset below, a grown or
        # refilled file falls through to the tail / full re-read.
        report.files_skipped += 1
        return

    start_offset = _start_offset_for(
        spec.source, path, size=size, mtime=stat.st_mtime, mode=mode, index=index
    )
    report.files_scanned += 1
    for row in _iter_rows_from(spec.source, path, start_offset=start_offset):
        _upsert_row_safe(store, spec, row, path, report)
    _stamp_file_meta(store, path, size=size, mtime=stat.st_mtime, index=index)


def _start_offset_for(
    source: SessionSource[object],
    path: Path,
    *,
    size: int,
    mtime: float,
    mode: RebuildMode,
    index: _FileMetaIndex,
) -> int:
    """Resolve the byte offset to start projecting *path* from.

    A FULL rebuild always starts at ``0``. In incremental mode the recorded
    ``last_offset`` is honoured unless one of three conditions forces a full
    re-read from ``0``:

    * the file has shrunk below the recorded offset — a rotated or truncated
      source whose old cursor now points past end-of-file;
    * the file is the same size as the recorded offset (cursor at EOF) but its
      mtime changed — an in-place rewrite / rotation that refilled to the exact
      previous byte count, so honouring the stale offset would tail-slice
      nothing and silently drop the fresh content; or
    * the source folds the whole file into one row rather than emitting one
      row per line, so a tail slice would re-parse a partial fragment and
      overwrite the complete row with a truncated one.

    Args:
        source: The adapter being driven (its ``source_name`` decides whether
            tail-slicing is safe).
        path: The source file being projected.
        size: The current file size in bytes.
        mtime: The current file modification time.
        mode: The rebuild mode.
        index: The in-memory file-meta cursor cache.

    Returns:
        The byte offset to begin projection at.
    """
    if mode is RebuildMode.FULL:
        return 0
    recorded = index.offset_for(str(path))
    recorded_mtime = index.mtime_for(str(path))
    rewritten_at_eof = recorded == size and recorded_mtime is not None and recorded_mtime != mtime
    if recorded > size or rewritten_at_eof:
        logger.info(
            f"_start_offset_for source={source.source_name} path={str(path)!r} "
            f"recorded={recorded} size={size} reset offset rotated source"
        )
        return 0
    if not _is_line_independent(source):
        return 0
    return recorded


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

    The caller (:func:`_start_offset_for`) only ever passes a non-zero
    ``start_offset`` for line-independent sources, so the tail slice here is
    always re-parseable in isolation; fold-whole-file adapters always arrive
    with ``start_offset == 0``.
    """
    if start_offset <= 0:
        yield from source.iter_rows(path)
        return
    with path.open("rb") as handle:
        handle.seek(start_offset)
        tail = handle.read()
    if not tail:
        return
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="eawf-telemetry-tail-", suffix=path.suffix, delete=False
        ) as tmp:
            tmp.write(tail)
            tmp.flush()
            tmp_path = Path(tmp.name)
        yield from source.iter_rows(tmp_path)
    finally:
        if tmp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def _upsert_row_safe(
    store: AbstractMetricsStore,
    spec: SourceSpec,
    row: object,
    path: Path,
    report: RebuildReport,
) -> None:
    """Upsert one row, logging + skipping it if the upsert fails.

    A single malformed or unhandled row must not discard the whole rebuild:
    the per-row upsert is isolated so a failure is logged, counted in
    ``report.rows_skipped``, and the scan continues — the good rows already
    accumulated stay in the pending transaction and commit at the end of the
    :func:`rebuild`. Without this isolation an exception would unwind past the
    single ``store.commit()`` and drop every projected row.

    A :class:`~eawf.observability.telemetry.aggregator.SessionPayloadSchemaError` is
    deliberately NOT isolated: an unprojectable payload version is a
    property of the whole log, not of one row, so swallowing it would
    report a clean rebuild over an empty session table.
    """
    try:
        _upsert_row(store, spec, row, report)
    except SessionPayloadSchemaError:
        raise
    except Exception as exc:
        report.rows_skipped += 1
        logger.warning(
            f"_upsert_row_safe source={spec.source.source_name} path={str(path)!r} "
            f"row_type={type(row).__name__!r} error={exc!r} skipped bad row"
        )


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
        session = session_from_envelope(row)
        if session is not None:
            store.upsert(_SESSIONS_TABLE, roll_session(session, project_id=spec.project_id))
            report.sessions += 1
        incident = incident_from_envelope(row)
        if incident is not None:
            store.upsert(_INCIDENTS_TABLE, incident)
            report.incidents += 1
        return
    if isinstance(row, TelemetryIncident):
        store.upsert(_INCIDENTS_TABLE, row)
        report.incidents += 1
        return
    if isinstance(row, TelemetryDispatchCost):
        store.upsert(_DISPATCH_COSTS_TABLE, row)
        report.dispatch_costs += 1
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
    mtime: float,
    index: _FileMetaIndex,
) -> None:
    """Advance the ``telemetry_file_meta`` cursor for *path* to end-of-file.

    The recorded ``size`` + ``mtime`` are the same values the scan decision in
    :func:`_project_file` read, so the next incremental skip gate compares
    against a snapshot consistent with the bytes just projected.
    """
    meta = TelemetryFileMeta(
        jsonl_path=str(path),
        mtime=mtime,
        size=size,
        last_offset=size,
        last_scan_ts=datetime.now(tz=UTC),
    )
    store.upsert(_FILE_META_TABLE, meta)
    index.by_path[meta.jsonl_path] = meta


class SweepStage(StrEnum):
    """The coverage-funnel stage a swept session file failed to reach."""

    PARSED = "parsed"
    ATTRIBUTED = "attributed"
    PROJECTED = "projected"


@dataclass(frozen=True, slots=True)
class SweepDrop:
    """One session file that left the funnel, named by its file name only.

    Attributes:
        file_name: Base name of the dropped file (never its directory).
        stage: The stage the file failed to reach.
        reason: Why it was dropped.
    """

    file_name: str
    stage: SweepStage
    reason: SweepDropReason


@dataclass(slots=True)
class SweepFunnel:
    """Coverage funnel of one session-history sweep over the whole index.

    The file stages count files that reached them, so the counts never
    increase from one stage to the next, and each file missing from a later
    stage has exactly one :class:`SweepDrop` saying why. A Claude subagent
    transcript is a file of its own here and bills its parent session. The
    request stages count the requests of every parsed file: each request is
    priced or dropped for exactly one :class:`RequestDropReason`. An
    unchanged file counts from its index row without being opened.

    Attributes:
        seen: Session files discovered.
        subagent_files: The seen files that are subagent transcripts.
        parsed: Files holding at least one decodable record.
        attributed: Parsed files with billed work to attribute.
        projected: Files whose session's rows were written.
        durations: Per-kind duration rows the projected sessions hold.
        drops: One entry per file that left the funnel.
        requests: Requests the parsed files hold.
        requests_priced: Requests that carry a cost.
        request_drops: Unpriced requests by reason.
        requests_shared: Requests a file skipped because another file of
            the same session billed them first; counted once, there.
        files_read: Files this sweep opened past their index check.
        lines_decoded: Lines this sweep decoded.
        sessions: The observed session rows of the projected files, which
            the pricing coverage is weighed over.
    """

    seen: int = 0
    subagent_files: int = 0
    parsed: int = 0
    attributed: int = 0
    projected: int = 0
    durations: int = 0
    drops: list[SweepDrop] = field(default_factory=list)
    requests: int = 0
    requests_priced: int = 0
    request_drops: dict[RequestDropReason, int] = field(default_factory=dict)
    requests_shared: int = 0
    files_read: int = 0
    lines_decoded: int = 0
    sessions: list[ObservedSession] = field(default_factory=list)

    def stages(self) -> tuple[int, int, int, int]:
        """Return ``(seen, parsed, attributed, projected)``."""
        return (self.seen, self.parsed, self.attributed, self.projected)

    def _count(self, file: HistoryFile, entry: HistoryIndexEntry | None) -> None:
        """Add one file's outcome, read from its index row, to the funnel."""
        self.seen += 1
        self.subagent_files += file.subagent
        drop = SweepDropReason.CORRUPT_RECORD if entry is None else entry.drop_reason
        if drop is SweepDropReason.CORRUPT_RECORD:
            self.drops.append(SweepDrop(file.path.name, SweepStage.PARSED, drop))
            return
        assert entry is not None
        self.parsed += 1
        self.requests += entry.requests
        self.requests_priced += entry.requests_priced
        self.requests_shared += entry.requests_shared
        for reason, count in (
            (RequestDropReason.UNPRICED_MODEL, entry.requests_unpriced_model),
            (RequestDropReason.UNPRICED_RUNTIME, entry.requests_unpriced_runtime),
        ):
            if count:
                self.request_drops[reason] = self.request_drops.get(reason, 0) + count
        if drop in (SweepDropReason.NO_OPERATOR_WORK, SweepDropReason.REFLECTION_HELPER):
            self.drops.append(SweepDrop(file.path.name, SweepStage.ATTRIBUTED, drop))
            return
        self.attributed += 1
        if drop is SweepDropReason.PROJECTION_REFUSED:
            self.drops.append(SweepDrop(file.path.name, SweepStage.PROJECTED, drop))
            return
        self.projected += 1


IndexProgress = Callable[[str, int, int], None]
"""Receives ``("index", done, total)`` as the sweep works through the files."""

INDEX_LOCK_WAIT_SECONDS: Final = 900.0
"""How long a run waits for another run indexing into the same history index."""


class IndexBusyError(RuntimeError):
    """Another run held the history index for longer than a run waits."""


@contextmanager
def history_index_lock(index_path: Path, *, on_wait: Callable[[], None]) -> Iterator[None]:
    """Hold the history index for one run, waiting while another run holds it.

    The sweep writes the whole index in one transaction, so a second run
    reading or writing the same index meanwhile would fail on SQLite's lock;
    it waits here instead, for up to :data:`INDEX_LOCK_WAIT_SECONDS`.

    Args:
        index_path: The history index database file.
        on_wait: Called once, before waiting, when another run holds it.

    Yields:
        Nothing; the index is this run's until the block exits.

    Raises:
        IndexBusyError: When the other run still holds it after the wait.
    """
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(portalock.acquire(index_path, timeout=0))
        except portalock.LockTimeout:
            on_wait()
            try:
                held.enter_context(portalock.acquire(index_path, timeout=INDEX_LOCK_WAIT_SECONDS))
            except portalock.LockTimeout as exc:
                raise IndexBusyError(
                    f"another reflect run held the session-history index for over "
                    f"{INDEX_LOCK_WAIT_SECONDS:.0f} s; try again when it finishes"
                ) from exc
        yield


def index_session_history(
    store: AbstractMetricsStore,
    files: Sequence[HistoryFile],
    *,
    project_id: str,
    progress: IndexProgress | None = None,
    prices: PriceBook | None = None,
) -> SweepFunnel:
    """Bring the observed collection up to date with *files*, reading only what changed.

    The sole writer of the observed tables. Each file has a row in the
    machine-local history index holding its size, mtime, consumed offset,
    head digest and resumable fold:

    * a file whose size and mtime match its row is not opened;
    * a file that grew, kept its head and did not move its mtime backwards
      resumes its fold and decodes only the appended complete lines;
    * any other file, and a new one, is read from byte 0 and the request
      rows its earlier read wrote are replaced.

    Each file writes its own request rows, under its lane. A session whose
    files changed then has its session and duration rows rebuilt from every
    one of its files, so a Claude session's subagent transcripts add their
    requests, tokens and durations to it. A trailing line with no newline is
    left for a later sweep.

    Args:
        store: An initialised metrics store.
        files: The history files to index; a session's own transcript
            before its subagent transcripts, so it bills a request both
            hold.
        project_id: Scope label stamped on each session row this sweep
            writes; reflect scopes match projects by ``cwd_digest`` instead.
        progress: Called at the start, the end and about every tenth file.
        prices: The prices to bill with; the process's active book by default.
            When their digest differs from the one the index stored, every
            stored request is re-priced from its token classes first, with no
            transcript read.

    Returns:
        The coverage funnel over every file in *files*.
    """
    book = prices if prices is not None else active_price_book()
    index = {
        row.path_digest: row
        for row in store.fetch_all(_HISTORY_INDEX_TABLE, HistoryIndexEntry)
        if isinstance(row, HistoryIndexEntry)
    }
    funnel = SweepFunnel()
    total = len(files)
    _report_progress(progress, 0, total)
    digests: list[str] = []
    touched = _reprice_if_stale(store, book)
    owners = _RequestOwners(store)
    for done, file in enumerate(files, start=1):
        digest = history_path_digest(file.path)
        digests.append(digest)
        held = index.get(digest)
        entry = _index_file(
            store, file, held, digest=digest, funnel=funnel, owners=owners, prices=book
        )
        if entry is not None and entry is not held:
            index[digest] = entry
            touched.update(
                (row.runtime, row.vendor_session_ref)
                for row in (held, entry)
                if row is not None and row.vendor_session_ref is not None
            )
        _report_progress(progress, done, total)
    _project_sessions(store, index, touched, project_id=project_id)
    store.commit()
    projected: set[tuple[str, str]] = set()
    for file, digest in zip(files, digests, strict=True):
        entry = index.get(digest)
        funnel._count(file, entry)
        if entry is not None and entry.drop_reason is None and entry.vendor_session_ref is not None:
            projected.add((entry.runtime, entry.vendor_session_ref))
    funnel.sessions = [
        row
        for row in store.fetch_all(_OBSERVED_SESSIONS_TABLE, ObservedSession)
        if isinstance(row, ObservedSession) and (row.runtime, row.vendor_session_ref) in projected
    ]
    funnel.durations = sum(
        1
        for row in store.fetch_all(_OBSERVED_DURATIONS_TABLE, ObservedDuration)
        if isinstance(row, ObservedDuration) and (row.runtime, row.vendor_session_ref) in projected
    )
    logger.info(
        f"index_session_history files={funnel.seen} subagent={funnel.subagent_files} "
        f"read={funnel.files_read} lines={funnel.lines_decoded} parsed={funnel.parsed} "
        f"attributed={funnel.attributed} projected={funnel.projected} "
        f"requests={funnel.requests} priced={funnel.requests_priced} "
        f"shared={funnel.requests_shared} drops={len(funnel.drops)}"
    )
    return funnel


def _reprice_if_stale(store: AbstractMetricsStore, prices: PriceBook) -> set[tuple[str, str]]:
    """Re-price every stored request when the prices changed since they were billed.

    Returns:
        The ``(runtime, vendor_session_ref)`` of every session whose requests
        were re-priced, so its session row is rebuilt; empty when the stored
        digest matches.
    """
    stored = store.fetch_where(_SCHEMA_META_TABLE, TelemetrySchemaMeta, key=_PRICE_DIGEST_KEY)
    if any(isinstance(row, TelemetrySchemaMeta) and row.value == prices.digest for row in stored):
        return set()
    touched: set[tuple[str, str]] = set()
    for row in store.fetch_all(_OBSERVED_REQUESTS_TABLE, ObservedRequest):
        if isinstance(row, ObservedRequest):
            store.upsert(_OBSERVED_REQUESTS_TABLE, reprice_request(row, prices))
            touched.add((row.runtime, row.vendor_session_ref))
    store.upsert(
        _SCHEMA_META_TABLE, TelemetrySchemaMeta(key=_PRICE_DIGEST_KEY, value=prices.digest)
    )
    logger.info(f"_reprice_if_stale sessions={len(touched)} digest={prices.version}")
    return touched


def _project_sessions(
    store: AbstractMetricsStore,
    index: dict[str, HistoryIndexEntry],
    touched: set[tuple[str, str]],
    *,
    project_id: str,
) -> None:
    """Rebuild each touched session's rows and mark its files' projection outcome."""
    by_session: dict[tuple[str, str], list[str]] = defaultdict(list)
    for digest, row in index.items():
        if row.vendor_session_ref is not None:
            by_session[(row.runtime, row.vendor_session_ref)].append(digest)
    for key in sorted(touched):
        written = _project_session(store, key, [index[d] for d in by_session[key]], project_id)
        for digest in by_session[key]:
            row = index[digest]
            if written and row.drop_reason is SweepDropReason.PROJECTION_REFUSED:
                drop = None
            elif not written and row.drop_reason is None:
                drop = SweepDropReason.PROJECTION_REFUSED
            else:
                continue
            index[digest] = row.model_copy(update={"drop_reason": drop})
            store.upsert(_HISTORY_INDEX_TABLE, index[digest])


def sweep_session_history(
    store: AbstractMetricsStore,
    history_root: Path,
    *,
    project_id: str,
) -> SweepFunnel:
    """Index the Claude transcripts of one project directory, subagents included.

    Args:
        store: An initialised metrics store.
        history_root: One project's Claude history directory; a missing
            directory sweeps nothing.
        project_id: Scope label stamped on every observed session row.

    Returns:
        The coverage funnel of the sweep.
    """
    files = discover_claude_project(history_root)
    return index_session_history(store, files, project_id=project_id)


def load_session_detail(
    store: AbstractMetricsStore,
    files: Sequence[HistoryFile],
    vendor_session_ref: str,
) -> SessionDetail | None:
    """Return one indexed session's detail, read from its own history file.

    The index keeps only path digests, so the file is found by digesting the
    candidate *files*. A subagent transcript is never read as the session:
    the session page draws subagents from the delegation spans of its own
    transcript.

    Args:
        store: The metrics store the session was indexed into.
        files: The history files to look among.
        vendor_session_ref: The session's hashed id.

    Returns:
        The detail, or ``None`` when no candidate file is the session's own.
    """
    digests = {
        row.path_digest
        for row in store.fetch_where(
            _HISTORY_INDEX_TABLE, HistoryIndexEntry, vendor_session_ref=vendor_session_ref
        )
        if isinstance(row, HistoryIndexEntry)
    }
    for file in files:
        if not file.subagent and history_path_digest(file.path) in digests:
            return read_session_detail(file)
    return None


@dataclass(frozen=True, slots=True)
class LocalHistory:
    """This machine's Claude and Codex session history, located once for one run.

    A reader that must not handle history paths itself indexes and reads
    sessions through this, never through the files it holds.

    Attributes:
        files: The history files, in the order the sweep reads them.
    """

    files: tuple[HistoryFile, ...]

    @classmethod
    def discover(cls) -> LocalHistory:
        """Return every Claude transcript and Codex rollout on this machine."""
        return cls(
            tuple(
                discover_history_files(
                    claude_projects=claude_projects_root(), codex_sessions=codex_sessions_root()
                )
            )
        )

    @property
    def file_count(self) -> int:
        """Return how many history files were found."""
        return len(self.files)

    def index(
        self,
        store: AbstractMetricsStore,
        *,
        project_id: str,
        progress: IndexProgress | None = None,
        prices: PriceBook | None = None,
    ) -> SweepFunnel:
        """Index the files into *store*; see :func:`index_session_history`."""
        return index_session_history(
            store, self.files, project_id=project_id, progress=progress, prices=prices
        )

    def detail(self, store: AbstractMetricsStore, vendor_session_ref: str) -> SessionDetail | None:
        """Return one indexed session's detail; see :func:`load_session_detail`."""
        return load_session_detail(store, self.files, vendor_session_ref)


def _report_progress(progress: IndexProgress | None, done: int, total: int) -> None:
    """Pass the count to *progress* at the start, the end and about every tenth file."""
    if progress is not None and (done in (0, total) or done % max(1, total // 10) == 0):
        progress("index", done, total)


def _head_digest(head: bytes) -> str:
    return hashlib.sha256(head).hexdigest()


def _resumable(handle: IO[bytes], entry: HistoryIndexEntry, *, size: int, mtime: float) -> bool:
    """Return whether the file only grew since *entry* was written."""
    if size < entry.byte_offset or mtime < entry.mtime:
        return False
    handle.seek(0)
    return _head_digest(handle.read(entry.head_bytes)) == entry.head_digest


def _index_file(
    store: AbstractMetricsStore,
    file: HistoryFile,
    entry: HistoryIndexEntry | None,
    *,
    digest: str,
    funnel: SweepFunnel,
    owners: _RequestOwners,
    prices: PriceBook,
) -> HistoryIndexEntry | None:
    """Bring one file's request rows and index row up to date.

    Returns:
        The file's index row, *entry* itself when the file is unchanged, or
        ``None`` when the file could not be read.
    """
    try:
        stat = file.path.stat()
    except OSError as exc:
        logger.warning(f"_index_file file={file.path.name!r} error={type(exc).__name__}")
        return None
    if (
        entry is not None
        and entry.runtime == file.runtime
        and entry.size == stat.st_size
        and entry.mtime == stat.st_mtime
    ):
        return entry
    try:
        with file.path.open("rb") as handle:
            fold, ledger, start = _resume_or_restart(
                store, owners, handle, file, entry, stat, prices
            )
            handle.seek(start)
            consumed, decoded = fold_history_lines(
                handle, fold, ledger, fallback_session_id=file.fallback_session_id
            )
            handle.seek(0)
            head = handle.read(HEAD_DIGEST_BYTES)
    except OSError as exc:
        logger.warning(f"_index_file file={file.path.name!r} error={type(exc).__name__}")
        return None
    funnel.files_read += 1
    funnel.lines_decoded += decoded
    for ref in sorted(ledger.changed):
        store.upsert(_OBSERVED_REQUESTS_TABLE, ledger.rows[ref])
    if fold.vendor_session_ref is not None:
        owners.claim(file.runtime, fold.vendor_session_ref, ledger.changed, lane=file.lane)
    offset = start + consumed
    requests = ledger.rows.values()
    priced = sum(1 for row in requests if row.price_source is not PriceSourceKind.UNPRICED)
    unpriced_runtime = sum(
        1
        for row in requests
        if row.price_source is PriceSourceKind.UNPRICED and row.runtime == "codex"
    )
    drop = None
    if not fold.records:
        drop = SweepDropReason.CORRUPT_RECORD
    elif fold.reflection_helper:
        drop = SweepDropReason.REFLECTION_HELPER
    elif not ledger.rows:
        drop = SweepDropReason.NO_OPERATOR_WORK
    updated = HistoryIndexEntry(
        path_digest=digest,
        runtime=file.runtime,
        size=max(stat.st_size, offset, len(head)),
        mtime=stat.st_mtime,
        byte_offset=offset,
        head_bytes=len(head),
        head_digest=_head_digest(head),
        vendor_session_ref=fold.vendor_session_ref,
        drop_reason=drop,
        requests=len(ledger.rows),
        requests_priced=priced,
        requests_unpriced_model=len(ledger.rows) - priced - unpriced_runtime,
        requests_unpriced_runtime=unpriced_runtime,
        requests_shared=len(fold.shared_refs),
        durations=len(fold.tallies),
        fold=fold.index_json(),
        last_scan_ts=datetime.now(tz=UTC),
    )
    store.upsert(_HISTORY_INDEX_TABLE, updated)
    return updated


def _session_requests(
    store: AbstractMetricsStore, runtime: str, vendor_session_ref: str
) -> list[ObservedRequest]:
    return [
        row
        for row in store.fetch_where(
            _OBSERVED_REQUESTS_TABLE,
            ObservedRequest,
            runtime=runtime,
            vendor_session_ref=vendor_session_ref,
        )
        if isinstance(row, ObservedRequest)
    ]


@dataclass(slots=True)
class _RequestOwners:
    """Which lane billed each request of a session, read once per session per sweep.

    A session with hundreds of subagent transcripts would otherwise re-read
    its whole request table once per file.
    """

    store: AbstractMetricsStore
    lanes: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)

    def _of(self, runtime: str, vendor_session_ref: str) -> dict[str, str]:
        key = (runtime, vendor_session_ref)
        if key not in self.lanes:
            self.lanes[key] = {
                row.request_ref: row.lane
                for row in _session_requests(self.store, runtime, vendor_session_ref)
            }
        return self.lanes[key]

    def foreign(self, runtime: str, vendor_session_ref: str, *, lane: str) -> frozenset[str]:
        """Return the requests of the session another lane billed."""
        held = self._of(runtime, vendor_session_ref)
        return frozenset(ref for ref, owner in held.items() if owner != lane)

    def claim(
        self, runtime: str, vendor_session_ref: str, refs: Iterable[str], *, lane: str
    ) -> None:
        """Record that *lane* billed *refs*."""
        self._of(runtime, vendor_session_ref).update(dict.fromkeys(refs, lane))

    def release(self, runtime: str, vendor_session_ref: str, *, lane: str) -> None:
        """Forget every request *lane* billed, after its rows were deleted."""
        held = self._of(runtime, vendor_session_ref)
        for ref in [ref for ref, owner in held.items() if owner == lane]:
            del held[ref]


def _resume_or_restart(
    store: AbstractMetricsStore,
    owners: _RequestOwners,
    handle: IO[bytes],
    file: HistoryFile,
    entry: HistoryIndexEntry | None,
    stat: os.stat_result,
    prices: PriceBook,
) -> tuple[TranscriptFold, RequestLedger, int]:
    """Return the fold, request rows and byte offset the read starts from.

    A file that only grew resumes from its stored fold and its own lane's
    rows. Any other file restarts at byte 0, and the request rows its
    earlier read wrote are deleted so the re-read replaces them instead of
    adding to them. Either way the ledger learns which requests the
    session's other files already billed.
    """
    if (
        entry is not None
        and entry.runtime == file.runtime
        and _resumable(handle, entry, size=stat.st_size, mtime=stat.st_mtime)
    ):
        fold = TranscriptFold.model_validate_json(entry.fold)
        ledger = RequestLedger(prices=prices)
        if entry.vendor_session_ref is not None:
            ledger.rows = {
                row.request_ref: row
                for row in store.fetch_where(
                    _OBSERVED_REQUESTS_TABLE,
                    ObservedRequest,
                    runtime=entry.runtime,
                    vendor_session_ref=entry.vendor_session_ref,
                    lane=file.lane,
                )
                if isinstance(row, ObservedRequest)
            }
            ledger.foreign = owners.foreign(entry.runtime, entry.vendor_session_ref, lane=file.lane)
        return fold, ledger, entry.byte_offset
    if entry is not None and entry.vendor_session_ref is not None:
        logger.info(f"_resume_or_restart file={file.path.name!r} reread_from=0 reason=rewritten")
        store.delete_where(
            _OBSERVED_REQUESTS_TABLE,
            runtime=entry.runtime,
            vendor_session_ref=entry.vendor_session_ref,
            lane=file.lane,
        )
        owners.release(entry.runtime, entry.vendor_session_ref, lane=file.lane)
    # A subagent's records name its parent session, whose own transcript is
    # indexed first; a request another file of the session already billed is
    # not billed again.
    ledger = RequestLedger(
        foreign=owners.foreign(
            file.runtime, hash_vendor_session_id(file.fallback_session_id), lane=file.lane
        ),
        prices=prices,
    )
    return TranscriptFold(runtime=file.runtime, lane=file.lane), ledger, 0


def _project_session(
    store: AbstractMetricsStore,
    key: tuple[str, str],
    entries: Sequence[HistoryIndexEntry],
    project_id: str,
) -> bool:
    """Rebuild one session's session and duration rows from all of its files.

    Args:
        store: The metrics store.
        key: The session's ``(runtime, vendor_session_ref)``.
        entries: The index rows of every file that folds into the session.
        project_id: Scope label stamped on the session row.

    Returns:
        ``False`` when the built rows failed their invariants and none were
        written, else ``True``.
    """
    runtime, ref = key
    store.delete_where(_OBSERVED_DURATIONS_TABLE, runtime=runtime, vendor_session_ref=ref)
    requests = _session_requests(store, runtime, ref)
    folds = [
        TranscriptFold.model_validate_json(entry.fold)
        for entry in entries
        if entry.drop_reason is not SweepDropReason.CORRUPT_RECORD
    ]
    if not requests or not folds:
        store.delete_where(_OBSERVED_SESSIONS_TABLE, runtime=runtime, vendor_session_ref=ref)
        return True
    try:
        session, durations = observed_rows(
            ParsedTranscript.merged(folds, requests), project_id=project_id
        )
    except ValueError as exc:
        logger.warning(f"_project_session error={exc!r} refused")
        store.delete_where(_OBSERVED_SESSIONS_TABLE, runtime=runtime, vendor_session_ref=ref)
        return False
    store.upsert(_OBSERVED_SESSIONS_TABLE, session)
    for row in durations:
        store.upsert(_OBSERVED_DURATIONS_TABLE, row)
    return True


__all__ = [
    "INDEX_LOCK_WAIT_SECONDS",
    "IndexBusyError",
    "IndexProgress",
    "LocalHistory",
    "RebuildMode",
    "RebuildReport",
    "SourceSpec",
    "SweepDrop",
    "SweepDropReason",
    "SweepFunnel",
    "SweepStage",
    "history_index_lock",
    "index_session_history",
    "load_session_detail",
    "rebuild",
    "sweep_session_history",
]
