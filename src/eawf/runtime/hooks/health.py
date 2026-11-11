"""The hook health record: one machine-local row per hook handler failure.

Hooks are fail-open, so a handler that raises or a daemon call that fails
must not break the host session; the failure is kept here instead of being
lost. Rows append to ``.ea/local/hooks/health.jsonl`` in the main checkout's
tree, where every worktree's hooks and the status line meet. Every row still
in the record is unacknowledged: an operator acknowledges the failures by
removing the record once they have read it.

A writer that finds the record at :data:`HEALTH_RECORD_CAP` bytes renames it
to ``health.jsonl.1``, dropping the older generation, so the newest rows stay.
Readers see the record as that generation followed by the current file.

Session start warns once about the rows written since the previous warning:
a byte-offset cursor beside the record marks how far the warning has read,
counted over both generations; a rotation moves it back by the bytes dropped.

The module imports no eawf package beyond its own because every hook process
and the status line load it.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

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

#: The size at which a writer rotates the record to :data:`HEALTH_ROTATED`, so a
#: hook that fails on every event (no runner, say) cannot grow it without bound.
HEALTH_RECORD_CAP: Final = 256 * 1024

#: The one older generation of the record a rotation keeps; the one before is dropped.
HEALTH_ROTATED: Final = HEALTH_RECORD.with_name(f"{HEALTH_RECORD.name}.1")


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
    _rotate_when_full(ea_dir)
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


def _rotate_when_full(ea_dir: Path) -> None:
    """Rotate the record once it reaches the cap; move the cursor back by the bytes dropped.

    The wrapper's bash writer rotates the same way. Two writers rotating at
    once can lose the rows one of them moved; a health record tolerates that.
    """
    path = ea_dir / HEALTH_RECORD
    try:
        if path.stat().st_size < HEALTH_RECORD_CAP:
            return
    except FileNotFoundError:
        return
    rotated = ea_dir / HEALTH_ROTATED
    dropped = rotated.stat().st_size if rotated.is_file() else 0
    os.replace(path, rotated)
    cursor = read_cursor(ea_dir)
    if cursor:
        write_cursor(ea_dir, max(0, cursor - dropped))


def _record_bytes(ea_dir: Path) -> bytes | None:
    """Return the older generation followed by the current record; ``None`` when neither exists."""
    parts: list[bytes] = []
    for relative in (HEALTH_ROTATED, HEALTH_RECORD):
        try:
            parts.append((ea_dir / relative).read_bytes())
        except FileNotFoundError:
            continue
    return b"".join(parts) if parts else None


def unacknowledged_count(ea_dir: Path) -> int:
    """Return how many failures the record under *ea_dir* holds, both generations.

    Args:
        ea_dir: The tree's ``.ea`` directory.

    Returns:
        The number of non-blank rows; ``0`` when there is no record.

    Raises:
        OSError: The record exists but cannot be read.
    """
    data = _record_bytes(ea_dir)
    return 0 if data is None else sum(1 for line in data.splitlines() if line.strip())


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
        session start reports only newer ones.

    Raises:
        OSError: The record or the cursor cannot be read or written.
    """
    cursor = read_cursor(ea_dir)
    read = read_health_rows(ea_dir, offset=cursor)
    if read.end != cursor:
        write_cursor(ea_dir, read.end)
    return failure_warning(read.rows)


__all__ = [
    "DAEMON_CALL_FAILED",
    "HEALTH_CURSOR",
    "HEALTH_RECORD",
    "HEALTH_RECORD_CAP",
    "HEALTH_ROTATED",
    "HOOK_RAISED",
    "HealthRead",
    "HealthRow",
    "HookFailure",
    "failure_line",
    "failure_warning",
    "read_cursor",
    "read_health_rows",
    "record_failures",
    "take_failure_warning",
    "unacknowledged_count",
    "write_cursor",
]
