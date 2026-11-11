"""The hook health record: one machine-local row per hook handler failure.

Hooks are fail-open, so a handler that raises or a daemon call that fails
must not break the host session; the failure is kept here instead of being
lost. Rows append to ``.ea/local/hooks/health.jsonl`` in the main checkout's
tree, where every worktree's hooks and the status line meet.

The status line counts the failures of the last 24 hours that came after the
operator's last acknowledgement, kept in ``ack.json`` beside the record. It
reads the record backward from its end and stops at the first older row, so a
long record costs a render no more than the rows it counts.

One rule bounds the record: a record larger than :data:`HEALTH_ROTATE_BYTES`
is renamed to ``health.1.jsonl``, replacing the older generation, so the
newest rows stay. A Python writer, the session-start warning and an
acknowledgement each apply it under the record's lock before they write; the
wrapper only appends, so when eawf cannot run at all the record grows by one
short row per hook event until the next Python hook that runs rotates it.
Readers see the record as the older generation followed by the current file
and read again when a rotation lands between their two reads.

Session start warns once about the rows written since the previous warning:
a byte-offset cursor beside the record marks how far the warning has read,
counted over both generations; a rotation moves it back by the bytes dropped.

The module imports no eawf package beyond its own at load time because every
hook process and the status line load it; only a rotating writer loads the
lock.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import orjson
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

#: The error code of a handler that raised.
HOOK_RAISED: Final = "hook_raised"

#: The error code of a daemon call a handler made that failed.
DAEMON_CALL_FAILED: Final = "daemon_call_failed"

#: Where the record lives, relative to a tree's ``.ea`` directory.
HEALTH_RECORD: Final = Path("local") / "hooks" / "health.jsonl"

#: The session-start warning's read cursor: the byte offset in the record up
#: to which the rows have been reported, relative to a tree's ``.ea``.
HEALTH_CURSOR: Final = HEALTH_RECORD.with_name("health.cursor")

#: The longest error detail a row keeps, so one runaway repr cannot bloat the record.
_DETAIL_CHARS: Final = 500

#: The record size above which a rotation moves it to :data:`HEALTH_ROTATED`, so
#: a hook that fails on every event (no daemon, say) cannot grow it without bound.
HEALTH_ROTATE_BYTES: Final = 1024 * 1024

#: The one older generation of the record a rotation keeps; the one before is dropped.
HEALTH_ROTATED: Final = HEALTH_RECORD.with_name("health.1.jsonl")

#: The older generation an earlier release rotated to, taken over on the next rotation.
_LEGACY_ROTATED: Final = HEALTH_RECORD.with_name(f"{HEALTH_RECORD.name}.1")

#: When the operator last acknowledged the failures, relative to a tree's ``.ea``.
HEALTH_ACK: Final = HEALTH_RECORD.with_name("ack.json")

#: How long a writer waits for the record's lock before it appends unrotated.
_LOCK_TIMEOUT_S: Final = 1.0

#: How often a reader reads again when a rotation lands between its reads.
_READ_ATTEMPTS: Final = 3

#: How far back the status line counts failures.
FAILURE_WINDOW: Final = timedelta(hours=24)

#: The most rows the status line counts, so one render reads a bounded tail.
WINDOW_ROWS: Final = 100

#: How many bytes a backward read takes from the record at a time.
_READ_BLOCK: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class HookFailure:
    """One failure of one hook handler.

    Attributes:
        event: The hook event value the handler ran for.
        hook: The registered handler name.
        code: :data:`HOOK_RAISED` or :data:`DAEMON_CALL_FAILED`.
        error: The exception class name.
        detail: The exception's ``repr``.
        at: When the failure was caught.
    """

    event: str
    hook: str
    code: str
    error: str
    detail: str
    at: datetime


def failure_line(failure: HookFailure) -> str:
    """Return the one stderr line a failure is reported by.

    The line names the event, the error code, the handler and the exception
    class, never the detail, which can carry machine paths.

    Args:
        failure: The failure to report.

    Returns:
        The line, without a trailing newline.
    """
    return f"eawf hook {failure.event}: {failure.code} in {failure.hook} ({failure.error})"


def record_failures(
    ea_dir: Path, failures: list[HookFailure], *, session_id: str | None = None
) -> int:
    """Append one health row per failure to the record under *ea_dir*.

    Each row is written by one append, so hook processes running side by
    side do not interleave their rows.

    Args:
        ea_dir: The tree's ``.ea`` directory.
        failures: The failures to record, in the order they happened.
        session_id: The host session the hook fired in, ``None`` when the
            payload named none.

    Returns:
        How many rows were appended: ``0`` when *failures* is empty or
        *ea_dir* is not a directory, because a hook fired outside an eawf
        tree has no record to write.

    Raises:
        OSError: The record cannot be created or written.
    """
    if not failures or not ea_dir.is_dir():
        return 0
    path = ea_dir / HEALTH_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_when_due(ea_dir)
    rows = b"".join(
        orjson.dumps(
            {
                "at": failure.at.isoformat(),
                "event": failure.event,
                "hook": failure.hook,
                "code": failure.code,
                "error": failure.error,
                "detail": failure.detail[:_DETAIL_CHARS],
                "session_id": session_id,
            }
        )
        + b"\n"
        for failure in failures
    )
    with path.open("ab") as handle:
        handle.write(rows)
    return len(failures)


@contextmanager
def _record_lock(ea_dir: Path) -> Iterator[bool]:
    """Hold the record's lock; yield whether it was taken within :data:`_LOCK_TIMEOUT_S`.

    A writer that cannot take it in time still appends, so a stuck holder
    costs a rotation, never a row.
    """
    from eawf.runtime.lock.portalock import LockTimeout, acquire

    try:
        with acquire(ea_dir / HEALTH_RECORD, timeout=_LOCK_TIMEOUT_S):
            yield True
            return
    except LockTimeout:
        pass
    yield False


def _rotate_when_due(ea_dir: Path) -> None:
    """Take the record's lock and rotate, only when :func:`_rotate` has work to do.

    The unlocked look is only a shortcut: :func:`_rotate` looks again under the
    lock, so two writers that both see a full record rotate it once.
    """
    current = _identity(ea_dir / HEALTH_RECORD)
    full = current is not None and current[1] > HEALTH_ROTATE_BYTES
    if not full and not (ea_dir / _LEGACY_ROTATED).is_file():
        return
    with _record_lock(ea_dir) as locked:
        if locked:
            _rotate(ea_dir)


def _rotate(ea_dir: Path) -> bool:
    """Apply the record's one rotation rule; the caller holds :func:`_record_lock`.

    An older generation an earlier release left as ``health.jsonl.1`` first
    becomes :data:`HEALTH_ROTATED`, replacing one an acknowledgement moved
    aside, whose rows were all acknowledged; readers counted it first, so the
    cursor's offsets keep their meaning. Then a record larger than
    :data:`HEALTH_ROTATE_BYTES` replaces the older generation, and the cursor
    moves back by the bytes dropped, clamped to what readers still see.

    Returns:
        Whether the record was rotated.
    """
    legacy = ea_dir / _LEGACY_ROTATED
    if legacy.is_file():
        os.replace(legacy, ea_dir / HEALTH_ROTATED)
    path = ea_dir / HEALTH_RECORD
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    if size <= HEALTH_ROTATE_BYTES:
        return False
    rotated = ea_dir / HEALTH_ROTATED
    dropped = rotated.stat().st_size if rotated.is_file() else 0
    os.replace(path, rotated)
    cursor = read_cursor(ea_dir)
    if cursor:
        write_cursor(ea_dir, min(max(0, cursor - dropped), rotated.stat().st_size))
    return True


def _identity(path: Path) -> tuple[int, int, int] | None:
    """Return what changes when *path* is replaced or grown; ``None`` when it is absent."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _record_bytes(ea_dir: Path) -> bytes | None:
    """Return the older generation followed by the current record; ``None`` when neither exists.

    A rotation between the two reads replaces the older generation, so the
    pair is read again until the older generation stands still; after
    :data:`_READ_ATTEMPTS` the last pair is kept, which can miss the rows of
    the rotation but never repeats one.
    """
    parts: list[bytes] = []
    for _ in range(_READ_ATTEMPTS):
        before = _identity(ea_dir / HEALTH_ROTATED)
        parts = []
        for relative in (HEALTH_ROTATED, HEALTH_RECORD):
            try:
                parts.append((ea_dir / relative).read_bytes())
            except FileNotFoundError:
                continue
        if _identity(ea_dir / HEALTH_ROTATED) == before:
            break
    return b"".join(parts) if parts else None


class HealthRow(BaseModel):
    """One row of the record, as the hooks and the wrapper write it.

    Attributes:
        at: When the failure was caught, ISO 8601.
        event: The hook event value.
        hook: The handler name; ``wrapper`` for a row the wrapper wrote.
        code: The error code.
        error: The error class or exit status.
        detail: The error detail, at most 500 characters.
        session_id: The host session; ``None`` on rows written before
            sessions were recorded, or when the payload named none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: str
    event: str
    hook: str
    code: str
    error: str
    detail: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class HealthRead:
    """The rows of the record from one byte offset on.

    Attributes:
        rows: The rows that parsed, oldest first.
        malformed: How many complete lines did not parse as a row.
        end: The byte offset just past the last complete line read; a line a
            writer has not finished is left for the next read.
    """

    rows: tuple[HealthRow, ...]
    malformed: int
    end: int


def read_health_rows(ea_dir: Path, *, offset: int = 0) -> HealthRead:
    """Return the rows of the record under *ea_dir* from byte *offset* on.

    Args:
        ea_dir: The tree's ``.ea`` directory.
        offset: Where to start reading, counted over the older generation
            followed by the current record. An offset past the end means the
            record was removed and written anew, so it reads from the start.

    Returns:
        The rows; no rows and ``end`` ``0`` when there is no record.

    Raises:
        ValueError: *offset* is negative.
        OSError: The record exists but cannot be read.
    """
    if offset < 0:
        raise ValueError(f"offset must not be negative; got {offset}")
    whole = _record_bytes(ea_dir)
    if whole is None:
        return HealthRead(rows=(), malformed=0, end=0)
    start = offset if offset <= len(whole) else 0
    data = whole[start:]
    complete = data[: data.rfind(b"\n") + 1]
    rows: list[HealthRow] = []
    malformed = 0
    for line in complete.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(HealthRow.model_validate_json(line))
        except ValidationError:
            malformed += 1
    return HealthRead(rows=tuple(rows), malformed=malformed, end=start + len(complete))


def read_cursor(ea_dir: Path) -> int:
    """Return the byte offset the session-start warning has read the record to.

    Args:
        ea_dir: The tree's ``.ea`` directory.

    Returns:
        The offset; ``0`` when there is no cursor or it does not hold a
        non-negative integer, so every row counts as unreported.

    Raises:
        OSError: The cursor exists but cannot be read.
    """
    try:
        text = (ea_dir / HEALTH_CURSOR).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0
    return int(text) if text.isdecimal() else 0


def write_cursor(ea_dir: Path, offset: int) -> None:
    """Move the session-start warning's cursor to *offset*.

    The cursor is replaced whole, so a session start racing another reads
    either offset, never a torn one.

    Args:
        ea_dir: The tree's ``.ea`` directory.
        offset: The byte offset the rows have been reported to.

    Raises:
        ValueError: *offset* is negative.
        OSError: The cursor cannot be written.
    """
    if offset < 0:
        raise ValueError(f"offset must not be negative; got {offset}")
    path = ea_dir / HEALTH_CURSOR
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_text(f"{offset}\n", encoding="utf-8")
    staged.replace(path)


def failure_warning(rows: tuple[HealthRow, ...]) -> str | None:
    """Return the session-start line reporting *rows*, if there are any.

    Args:
        rows: The failures not yet reported, oldest first.

    Returns:
        ``eawf: <N> hook failures since the last session (top: <code> x<n>);
        run eawf doctor hooks``, where the top code is the most frequent one
        and the earliest of equally frequent ones; ``None`` for no rows.
    """
    if not rows:
        return None
    code, count = Counter(row.code for row in rows).most_common(1)[0]
    return (
        f"eawf: {len(rows)} hook failures since the last session "
        f"(top: {code} x{count}); run eawf doctor hooks"
    )


def take_failure_warning(ea_dir: Path) -> str | None:
    """Return the warning for the rows written since the last one and advance the cursor.

    Args:
        ea_dir: The main checkout's ``.ea`` directory.

    Returns:
        The warning line, or ``None`` when no row was written since the
        cursor; the cursor then moves past every complete row, so the next
        session start reports only newer ones. ``None`` too when another
        writer holds the record's lock past the timeout; the rows wait for
        the next session start.

    Raises:
        OSError: The record or the cursor cannot be read or written.
    """
    # Under the lock no rotation lands between the read and the cursor write,
    # so the stored offset always belongs to the layout it was read from.
    with _record_lock(ea_dir) as locked:
        if not locked:
            return None
        _rotate(ea_dir)
        cursor = read_cursor(ea_dir)
        read = read_health_rows(ea_dir, offset=cursor)
        if read.end != cursor:
            write_cursor(ea_dir, read.end)
    return failure_warning(read.rows)


class HealthAck(BaseModel):
    """The acknowledgement file: the failures up to this instant have been read.

    Attributes:
        acknowledged_at: When the operator acknowledged, with its offset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledged_at: AwareDatetime


def read_ack(ea_dir: Path) -> datetime | None:
    """Return when the failures under *ea_dir* were last acknowledged.

    Args:
        ea_dir: The main checkout's ``.ea`` directory.

    Returns:
        The instant; ``None`` when nothing was ever acknowledged, or when the
        file cannot be read or is not a :class:`HealthAck` (garbage, or a
        time without its offset). A corrupt acknowledgement acknowledges
        nothing, so it can never silence the failures it was meant to cover.
    """
    try:
        return HealthAck.model_validate_json((ea_dir / HEALTH_ACK).read_bytes()).acknowledged_at
    except OSError, ValueError:
        return None


def _lines_backward(path: Path) -> Iterator[bytes]:
    """Yield the complete lines of *path*, newest first; nothing when it is absent.

    The text after the last newline is skipped: it is a row a writer has not
    finished appending.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        position = handle.seek(0, os.SEEK_END)
        carry = b""
        unfinished = True
        while position > 0:
            step = min(_READ_BLOCK, position)
            position -= step
            handle.seek(position)
            pieces = (handle.read(step) + carry).split(b"\n")
            carry = pieces.pop(0)
            for piece in reversed(pieces):
                if unfinished:
                    unfinished = False
                    continue
                yield piece
        if not unfinished:
            yield carry


def _row_instant(line: bytes) -> datetime | None:
    """Return when the row on *line* was written, or ``None`` when it is no row.

    The wrapper and the hooks both write UTC offsets; a row without one is
    read as UTC rather than dropped.
    """
    try:
        at = datetime.fromisoformat(HealthRow.model_validate_json(line).at)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _count_since(ea_dir: Path, since: datetime, *, limit: int | None) -> int:
    """Count the rows after *since*, newest first, stopping at an older row or *limit*.

    A rotation during the count would show the moved rows twice, so the count
    is taken again until the older generation stands still.
    """
    count = 0
    for _ in range(_READ_ATTEMPTS):
        before = _identity(ea_dir / HEALTH_ROTATED)
        count = _count_once(ea_dir, since, limit=limit)
        if _identity(ea_dir / HEALTH_ROTATED) == before:
            break
    return count


def _count_once(ea_dir: Path, since: datetime, *, limit: int | None) -> int:
    """Count once over the current record, then the older generation."""
    count = 0
    for relative in (HEALTH_RECORD, HEALTH_ROTATED):
        for line in _lines_backward(ea_dir / relative):
            at = _row_instant(line) if line.strip() else None
            if at is None:
                continue
            if at <= since:
                return count
            count += 1
            if count == limit:
                return count
    return count


def _window_start(acknowledged: datetime | None, now: datetime) -> datetime:
    """Return the later of *acknowledged* and the start of the window ending at *now*."""
    window = now - FAILURE_WINDOW
    return window if acknowledged is None else max(acknowledged, window)


def recent_failure_count(ea_dir: Path, *, now: datetime) -> int:
    """Return how many failures the status line shows for the record under *ea_dir*.

    A failure counts when it was written after both the last acknowledgement
    and :data:`FAILURE_WINDOW` before *now*. The record is read from its end
    and the read stops at the first older row or at :data:`WINDOW_ROWS` rows.

    Args:
        ea_dir: The main checkout's ``.ea`` directory.
        now: The current instant, with its offset.

    Returns:
        The count, from ``0`` to :data:`WINDOW_ROWS`; ``0`` when there is no record.

    Raises:
        OSError: The record cannot be read.
    """
    return _count_since(ea_dir, _window_start(read_ack(ea_dir), now), limit=WINDOW_ROWS)


def acknowledge(ea_dir: Path, *, now: datetime) -> int:
    """Acknowledge every failure recorded under *ea_dir* up to *now*.

    Writes :data:`HEALTH_ACK`, replaced whole so a render never reads a torn
    file, then applies the record's one rotation rule under its lock: a
    record larger than :data:`HEALTH_ROTATE_BYTES` becomes
    :data:`HEALTH_ROTATED`, replacing any older one, and the session-start
    cursor is clamped to what readers still see.

    Args:
        ea_dir: The main checkout's ``.ea`` directory.
        now: The acknowledgement instant, with its offset.

    Returns:
        How many failures of the window this acknowledged, uncapped. An
        unreadable earlier acknowledgement counts as none, since this one
        replaces it.

    Raises:
        FileNotFoundError: *ea_dir* is not a directory: no managed tree is there.
        ValueError: *now* has no offset.
        OSError: The record cannot be read or the acknowledgement written.
    """
    if not ea_dir.is_dir():
        raise FileNotFoundError(f"no eawf tree at {ea_dir}")
    stamp = HealthAck(acknowledged_at=now).acknowledged_at.astimezone(UTC)
    count = _count_since(ea_dir, _window_start(read_ack(ea_dir), stamp), limit=None)
    path = ea_dir / HEALTH_ACK
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_bytes(HealthAck(acknowledged_at=stamp).model_dump_json().encode("utf-8"))
    staged.replace(path)
    _rotate_when_due(ea_dir)
    return count


__all__ = [
    "DAEMON_CALL_FAILED",
    "FAILURE_WINDOW",
    "HEALTH_ACK",
    "HEALTH_CURSOR",
    "HEALTH_RECORD",
    "HEALTH_ROTATED",
    "HEALTH_ROTATE_BYTES",
    "HOOK_RAISED",
    "WINDOW_ROWS",
    "HealthAck",
    "HealthRead",
    "HealthRow",
    "HookFailure",
    "acknowledge",
    "failure_line",
    "failure_warning",
    "read_ack",
    "read_cursor",
    "read_health_rows",
    "recent_failure_count",
    "record_failures",
    "take_failure_warning",
    "write_cursor",
]
