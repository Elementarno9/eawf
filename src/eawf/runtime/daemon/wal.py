"""Outcome-WAL record lifecycle for the daemon mutator path.

The WAL persists the **post-apply envelope** of a state mutation so the
daemon can recover from a SIGKILL between ``state.json`` rewrite and
``event.jsonl`` append. Records move through four statuses on disk via
atomic renames:

- ``pending``  — mutator wrote the WAL record before fsyncing state +
  event log. A crash here leaves a ``.pending.json`` file that startup
  replay treats as failed (operator-investigable via ``poisoned/``).
- ``applied``  — state + event were written; the rename to
  ``.applied.json`` lands before fsync. A crash here leaves a record
  that replay can re-issue verbatim (envelope is captured in full).
- ``fsynced``  — state + event were fsynced; the record can be GC'd
  after the retention window.
- ``poisoned`` — replay (or the operator) marked the record
  unrecoverable; lives under ``poisoned/`` with a ``poison_reason``.

The WAL primitive does NOT execute the mutator on replay: it stores the
fully-applied envelope and replay re-issues that exact envelope. This
side-steps the non-determinism in ``apply`` (``datetime.now()``, UUID
gen, ``git rev-parse HEAD``) that an intent-WAL would re-run on every
recovery and produce diverging event ids.

Atomic-write strategy mirrors :mod:`eawf.kernel.state.writer`: tempfile +
``os.replace`` + parent-dir fsync. Renames between status suffixes are
plain ``os.replace`` calls — atomic on POSIX + Windows.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.fsync import fsync_parent_dir
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


#: Genesis-record sentinel for :attr:`WalRecord.prev_digest`. A record with
#: no chained predecessor links to this sentinel so the chain has a single,
#: well-known root and :func:`verify_record_digest` can distinguish "first
#: record" from "tampered ``prev_digest``".
WAL_CHAIN_GENESIS: str = "genesis"

#: Poison reason recorded when replay detects a record whose stored
#: :attr:`WalRecord.digest` does not match the digest recomputed over its
#: content -- the demonstrated V1 integrity hole. Replay refuses such a
#: record (no event row appended) and moves it under ``poisoned/``.
WAL_DIGEST_MISMATCH_REASON: str = "wal_digest_mismatch"


_DONE_RETENTION_SECONDS_DEFAULT: int = 3600

#: Default interval (seconds) between background WAL-GC sweeps. Hourly is
#: granular enough for the 1-hour default retention window -- the sweep is a
#: janitor for already-durable ``.fsynced.json`` records, not a hot path.
DEFAULT_WAL_GC_INTERVAL_SECONDS: int = 3600

#: Env var that overrides the WAL-GC sweep interval (seconds).
_WAL_GC_INTERVAL_ENV: str = "EAWF_DAEMON_WAL_GC_INTERVAL"

#: Env var that overrides the WAL retention window (seconds) before a
#: fsynced record is eligible for GC.
_WAL_RETENTION_ENV: str = "EAWF_DAEMON_WAL_RETENTION"


class WalStatus(StrEnum):
    """Lifecycle status of a single WAL record on disk."""

    PENDING = "pending"
    APPLIED = "applied"
    FSYNCED = "fsynced"
    POISONED = "poisoned"


class WalRecord(BaseModel):
    """Single outcome-WAL record.

    The :attr:`envelope` field carries the **post-apply** event envelope
    in full — the same payload that lands in ``event.jsonl`` after a
    successful mutation. Replay never re-executes the mutator; it
    re-issues this exact envelope when the corresponding event row is
    missing from the log.

    Attributes:
        record_id: WAL record id (typically a fresh uuid4); names the
            on-disk file ``<record_id>.<status>.json``.
        envelope: Post-apply event envelope captured by the mutator.
        idempotency_key: Optional client-supplied key for the V5
            cross-runtime retry path; stored verbatim for the
            idempotency cache that W08/W09 wires on top.
        written_at: When the WAL record was first persisted (the
            ``pending`` write). Sortable across records.
        before_state_version: Digest / version string of ``state.json``
            before the mutation applied. Lets replay detect crash-
            before-state-write vs crash-after-state-write.
        after_state_version: Digest / version string of ``state.json``
            after the mutation applied. Recorded so replay can match
            on-disk state against the WAL.
        poison_reason: Reason text injected by :func:`mark_poisoned`
            when the record is moved under ``poisoned/``.
        prev_digest: :attr:`digest` of the record that precedes this one
            in the WAL chain, or :data:`WAL_CHAIN_GENESIS` for the first
            record. Links every record back to its predecessor so a
            silent deletion or reorder of an earlier record breaks the
            chain at the next link.
        digest: SHA-256 over the record's canonical content (every field
            except :attr:`digest` and :attr:`poison_reason`). Computed at
            construction time and verified on replay -- a tampered record
            body recomputes to a different digest than the one stored on
            disk, which replay refuses (see
            :func:`eawf.runtime.daemon.recovery.replay_wal`).
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(min_length=1)
    envelope: Envelope
    idempotency_key: str | None = None
    written_at: UtcDatetime
    before_state_version: str
    after_state_version: str
    poison_reason: str | None = None
    prev_digest: str = WAL_CHAIN_GENESIS
    digest: str | None = None

    @model_validator(mode="after")
    def _stamp_digest(self) -> WalRecord:
        """Stamp :attr:`digest` over the record's canonical content when absent.

        The digest is computed once, at construction, over every field
        except ``digest`` itself and the mutable ``poison_reason`` (which
        :func:`mark_poisoned` injects after the fact via ``model_copy``,
        without re-validating). Excluding ``poison_reason`` keeps the
        digest stable across the poison move so a poisoned record still
        verifies against its original content.

        A record read back from disk already carries its stamped
        ``digest``; the validator leaves a non-``None`` value untouched so
        :func:`verify_record_digest` can compare the stored digest against
        a fresh recompute and catch a tampered body.
        """
        if self.digest is None:
            object.__setattr__(self, "digest", compute_record_digest(self))
        return self


#: Record fields the digest is computed over. Excludes ``digest`` (the
#: field being computed) and ``poison_reason`` (injected post-hoc by
#: :func:`mark_poisoned`, so including it would invalidate the digest of an
#: otherwise-intact poisoned record).
_DIGEST_EXCLUDED_FIELDS: set[str] = {"digest", "poison_reason"}


def compute_record_digest(record: WalRecord) -> str:
    """Return the SHA-256 hex digest of *record*'s canonical content.

    The digest covers every field except ``digest`` and ``poison_reason``
    (see :data:`_DIGEST_EXCLUDED_FIELDS`). Serialisation is the same
    deterministic ``orjson.OPT_SORT_KEYS`` dump the on-disk record uses,
    so the digest is stable across processes and re-reads.

    Args:
        record: The WAL record to hash.

    Returns:
        The 64-char hex SHA-256 digest over the canonical content bytes.
    """
    payload = record.model_dump(mode="json", exclude=_DIGEST_EXCLUDED_FIELDS)
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


def verify_record_digest(record: WalRecord) -> bool:
    """Return ``True`` iff *record*'s stored digest matches a fresh recompute.

    A record whose on-disk body was tampered with (any digested field
    edited) recomputes to a different digest than the one persisted in its
    :attr:`WalRecord.digest`, so this returns ``False`` -- the signal
    replay uses to refuse the poisoned record. A record with no stored
    digest (``digest is None``) cannot be verified and returns ``False``
    so a stripped digest is treated as tampering, not a pass.

    Args:
        record: The WAL record loaded from disk.

    Returns:
        ``True`` when the stored digest matches the recomputed digest.
    """
    stored = record.digest
    if stored is None:
        return False
    return stored == compute_record_digest(record)


def _record_filename(record_id: str, status: WalStatus) -> str:
    """Return the canonical on-disk filename for a WAL record."""
    return f"{record_id}.{status.value}.json"


def _record_path(wal_dir: Path, record_id: str, status: WalStatus) -> Path:
    """Return the on-disk path for ``(record_id, status)`` under *wal_dir*."""
    return wal_dir / _record_filename(record_id, status)


def _poisoned_path(wal_dir: Path, record_id: str) -> Path:
    """Return the poisoned-subdir path for *record_id*."""
    return wal_dir / "poisoned" / f"{record_id}.poisoned.json"


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Tempfile + ``os.replace`` + parent-dir fsync. Lock-agnostic.

    Mirrors :func:`eawf.kernel.state.writer._write_payload` but specialised for
    the WAL: no sibling lock (each WAL record is a unique file; no
    concurrent writer competes for the same path).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        fsync_parent_dir(target)
    finally:
        tmp.unlink(missing_ok=True)


def _serialise(record: WalRecord) -> bytes:
    """Serialise *record* to deterministic JSON bytes."""
    payload = record.model_dump(mode="json")
    return orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"


def _read_record(path: Path) -> WalRecord:
    """Read + validate a WAL record from *path*.

    Raises:
        ValueError: When *path* contains non-JSON bytes or fails
            ``WalRecord`` schema validation. The caller surfaces this
            via the ``poisoned/`` rename so the operator can inspect.
    """
    raw = path.read_bytes()
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise ValueError(f"wal record not valid json: {path!r}") from exc
    return WalRecord.model_validate(payload)


def write_pending(wal_dir: Path, record: WalRecord) -> Path:
    """Write *record* atomically as ``<record_id>.pending.json``.

    Args:
        wal_dir: Directory the WAL lives under (typically
            ``<runtime_dir>/wal/``). Created if missing.
        record: Fully populated :class:`WalRecord`.

    Returns:
        Path to the freshly written ``.pending.json`` file.

    Raises:
        FileExistsError: When a ``.pending.json`` for the same
            ``record_id`` already exists. Replay or operator-driven
            cleanup must resolve the prior record first.
    """
    target = _record_path(wal_dir, record.record_id, WalStatus.PENDING)
    if target.exists():
        raise FileExistsError(f"wal pending record already exists: {target!r}")
    _atomic_write_bytes(target, _serialise(record))
    logger.info(f"write_pending record={record.record_id!r} envelope_id={record.envelope.id!r}")
    return target


def _rename_status(
    wal_dir: Path, record_id: str, from_status: WalStatus, to_status: WalStatus
) -> Path:
    """Rename ``<record_id>.<from>.json`` → ``<record_id>.<to>.json``.

    Returns:
        Path to the post-rename file.

    Raises:
        FileNotFoundError: When the source file is missing — caller
            is operating on a record outside its expected lifecycle.
    """
    src = _record_path(wal_dir, record_id, from_status)
    dst = _record_path(wal_dir, record_id, to_status)
    if not src.exists():
        raise FileNotFoundError(
            f"wal record missing for rename: id={record_id!r} from={from_status.value}"
        )
    os.replace(src, dst)
    return dst


def mark_applied(wal_dir: Path, record_id: str) -> Path:
    """Rename ``<id>.pending.json`` → ``<id>.applied.json``.

    The caller is responsible for ordering: state.json is written before
    this rename (the durable point of no return); event.jsonl is appended
    AFTER, so a crash between this rename and the event append leaves an
    APPLIED record that :func:`eawf.runtime.daemon.recovery.replay_wal` re-issues.
    fsync happens in :func:`mark_fsynced`, after the event append.
    """
    dst = _rename_status(wal_dir, record_id, WalStatus.PENDING, WalStatus.APPLIED)
    logger.info(f"mark_applied record={record_id!r}")
    return dst


def mark_fsynced(wal_dir: Path, record_id: str) -> Path:
    """Rename ``<id>.applied.json`` → ``<id>.fsynced.json``.

    Caller MUST have fsynced state.json + event.jsonl before invoking;
    only then is the transaction durably committed and the WAL record
    eligible for GC.
    """
    dst = _rename_status(wal_dir, record_id, WalStatus.APPLIED, WalStatus.FSYNCED)
    logger.info(f"mark_fsynced record={record_id!r}")
    return dst


def mark_poisoned(wal_dir: Path, record_id: str, reason: str) -> Path:
    """Move any-status record for *record_id* to ``poisoned/<id>.poisoned.json``.

    Injects *reason* into the record body's :attr:`WalRecord.poison_reason`
    before re-serialising. Searches for the source file in status order
    ``pending`` → ``applied`` → ``fsynced``; raises
    :class:`FileNotFoundError` when none of the three exist.

    Args:
        wal_dir: Directory the WAL lives under.
        record_id: WAL record id to poison.
        reason: Short snake_case reason recorded on the record body so
            ``eawf daemon replay-wal --inspect`` can surface it.

    Returns:
        Path to the freshly written ``poisoned/<id>.poisoned.json``.

    Raises:
        FileNotFoundError: When no source record exists in any of the
            three live statuses.
        ValueError: When the source record fails schema validation
            and a reason cannot be injected. The bytes are still moved
            into ``poisoned/`` via the raw-rename branch so the
            operator can inspect.
    """
    dst = _poisoned_path(wal_dir, record_id)
    dst.parent.mkdir(parents=True, exist_ok=True)
    for status in (WalStatus.PENDING, WalStatus.APPLIED, WalStatus.FSYNCED):
        src = _record_path(wal_dir, record_id, status)
        if not src.exists():
            continue
        try:
            record = _read_record(src)
        except ValueError:
            # Raw move so the corrupt bytes survive for inspection. We
            # cannot inject the reason into a body we cannot parse.
            os.replace(src, dst)
            logger.warning(
                f"mark_poisoned record={record_id!r} reason={reason!r} parse_failed=true"
            )
            return dst
        record = record.model_copy(update={"poison_reason": reason})
        _atomic_write_bytes(dst, _serialise(record))
        src.unlink(missing_ok=True)
        logger.warning(f"mark_poisoned record={record_id!r} reason={reason!r}")
        return dst
    raise FileNotFoundError(f"wal record missing for poison: id={record_id!r}")


def gc_done_records(
    wal_dir: Path,
    max_age_seconds: int = _DONE_RETENTION_SECONDS_DEFAULT,
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Unlink ``.fsynced.json`` files older than *max_age_seconds*.

    The retention window keeps a debugging buffer of recently-completed
    mutations so an operator can correlate a state change with its
    WAL record. ``.pending.json`` and ``.applied.json`` are left alone
    (they live in the active-recovery window).

    Args:
        wal_dir: Directory the WAL lives under. A missing directory
            returns an empty list (no records to GC).
        max_age_seconds: Files whose mtime is older than ``now -
            max_age_seconds`` are unlinked. Default matches the
            retention window of 1 hour.
        now: Reference time the cutoff anchors on. Defaults to
            ``datetime.now(UTC)`` -- the background sweep loop injects a
            clock so a fake-clock test can drive the boundary
            deterministically.

    Returns:
        Paths that were unlinked, in lexical order. Useful for tests
        and the ``eawf daemon replay-wal --gc`` operator surface.
    """
    if not wal_dir.exists():
        return []
    reference = (now or datetime.now(UTC)).timestamp()
    threshold = reference - max_age_seconds
    removed: list[Path] = []
    for path in sorted(wal_dir.glob(f"*.{WalStatus.FSYNCED.value}.json")):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime <= threshold:
            path.unlink(missing_ok=True)
            removed.append(path)
    logger.info(f"gc_done_records removed={len(removed)} max_age_seconds={max_age_seconds}")
    return removed


def resolve_wal_gc_interval_seconds() -> int:
    """Return the configured WAL-GC sweep interval in seconds.

    The env var ``EAWF_DAEMON_WAL_GC_INTERVAL`` lets the operator override
    the default for testing + tuning while the layered-config daemon reader
    is still landing. A non-positive or unparseable override falls back to
    :data:`DEFAULT_WAL_GC_INTERVAL_SECONDS` and logs a warning -- a
    zero-or-negative interval must never spin a zero-sleep loop.

    Returns:
        Positive sweep interval in seconds.
    """
    return _resolve_positive_int_env(_WAL_GC_INTERVAL_ENV, DEFAULT_WAL_GC_INTERVAL_SECONDS)


def resolve_wal_retention_seconds() -> int:
    """Return the configured WAL retention window in seconds.

    The env var ``EAWF_DAEMON_WAL_RETENTION`` lets the operator override the
    default. A non-positive or unparseable override falls back to
    :data:`_DONE_RETENTION_SECONDS_DEFAULT` and logs a warning -- a
    zero-or-negative retention must never silently disable GC by widening
    the window to nothing or treating every record as in-window.

    Returns:
        Positive retention window in seconds.
    """
    return _resolve_positive_int_env(_WAL_RETENTION_ENV, _DONE_RETENTION_SECONDS_DEFAULT)


def _resolve_positive_int_env(env_var: str, default: int) -> int:
    """Resolve *env_var* to a positive int, falling back to *default*.

    Mirrors the daemon-main resolver pattern: read the raw value, parse it
    as an int, and reject a non-positive or unparseable value back to
    *default* with a warning so a misconfigured env cannot disable GC or
    spin a zero-interval loop.

    Args:
        env_var: Environment variable name to read.
        default: Fallback when the var is unset, unparseable, or
            non-positive.

    Returns:
        The parsed positive int, or *default*.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"_resolve_positive_int_env unparseable env={env_var!r} raw={raw!r}")
        return default
    if value <= 0:
        logger.warning(f"_resolve_positive_int_env non-positive env={env_var!r} raw={raw!r}")
        return default
    return value


async def run_wal_gc_loop(
    *,
    wal_dir: Path,
    retention_seconds: int = _DONE_RETENTION_SECONDS_DEFAULT,
    interval_seconds: int = DEFAULT_WAL_GC_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sweep aged ``.fsynced.json`` WAL records on a loop until stopped.

    Each tick unlinks fsynced records older than *retention_seconds* via
    :func:`gc_done_records`, then waits *interval_seconds* (or exits early
    when *stop_event* is set). The WAL would otherwise grow unbounded:
    nothing GCs the durable-and-eligible records once the daemon is past
    the active-recovery window for each mutation.

    Args:
        wal_dir: Directory the WAL lives under.
        retention_seconds: Records older than this are swept. Defaults to
            :data:`_DONE_RETENTION_SECONDS_DEFAULT`.
        interval_seconds: Seconds between sweeps. Defaults to
            :data:`DEFAULT_WAL_GC_INTERVAL_SECONDS`.
        stop_event: When set, the loop exits at the next tick. ``None``
            means the loop runs forever -- used by the daemon main
            entrypoint, never by tests.
        now: Clock factory injected for deterministic fake-clock tests;
            forwarded to :func:`gc_done_records`. Defaults to wall clock.
        sleep: Sleep coroutine factory used only when *stop_event* is
            ``None``; the stop-event path waits on the event instead.

    Raises:
        ValueError: When ``interval_seconds`` or ``retention_seconds`` is
            non-positive -- the caller must resolve a positive window so
            the loop cannot spin or disable GC.
    """
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be positive: {interval_seconds!r}")
    if retention_seconds <= 0:
        raise ValueError(f"retention_seconds must be positive: {retention_seconds!r}")
    stop = stop_event
    while stop is None or not stop.is_set():
        try:
            gc_done_records(
                wal_dir,
                retention_seconds,
                now=now() if now is not None else None,
            )
        except Exception:
            logger.exception("run_wal_gc_loop sweep failed; will retry next tick")
        if stop is None:
            await sleep(interval_seconds)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
        else:
            return


def list_records(wal_dir: Path, status: WalStatus | None = None) -> list[Path]:
    """Return WAL record paths sorted by record ``written_at``.

    Records whose body fails to parse sort to the **end** of the
    returned list (so callers see the recoverable records first); their
    relative order is by filename.

    Args:
        wal_dir: Directory the WAL lives under. Missing directory
            yields an empty list.
        status: When set, restrict to records in that status. When
            ``None`` walk every live status (excludes ``poisoned/``;
            list that subdirectory directly via :func:`list_poisoned`).

    Returns:
        Sorted list of paths to WAL record files.
    """
    if not wal_dir.exists():
        return []
    statuses: tuple[WalStatus, ...]
    if status is None:
        statuses = (WalStatus.PENDING, WalStatus.APPLIED, WalStatus.FSYNCED)
    else:
        statuses = (status,)
    candidates: list[Path] = []
    for s in statuses:
        candidates.extend(wal_dir.glob(f"*.{s.value}.json"))
    parseable: list[tuple[datetime, Path]] = []
    unparseable: list[Path] = []
    for path in candidates:
        try:
            record = _read_record(path)
        except ValueError, OSError:
            unparseable.append(path)
            continue
        parseable.append((record.written_at, path))
    parseable.sort(key=lambda pair: (pair[0], pair[1].name))
    unparseable.sort()
    return [p for _, p in parseable] + unparseable


def list_poisoned(wal_dir: Path) -> list[Path]:
    """Return ``poisoned/*.poisoned.json`` paths sorted lexically."""
    poisoned_dir = wal_dir / "poisoned"
    if not poisoned_dir.exists():
        return []
    return sorted(poisoned_dir.glob("*.poisoned.json"))


def read_record(path: Path) -> WalRecord:
    """Public alias for :func:`_read_record` — used by admin/CLI surfaces."""
    return _read_record(path)


__all__ = [
    "DEFAULT_WAL_GC_INTERVAL_SECONDS",
    "WAL_CHAIN_GENESIS",
    "WAL_DIGEST_MISMATCH_REASON",
    "WalRecord",
    "WalStatus",
    "compute_record_digest",
    "gc_done_records",
    "list_poisoned",
    "list_records",
    "mark_applied",
    "mark_fsynced",
    "mark_poisoned",
    "read_record",
    "resolve_wal_gc_interval_seconds",
    "resolve_wal_retention_seconds",
    "run_wal_gc_loop",
    "verify_record_digest",
    "write_pending",
]
