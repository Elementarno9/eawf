"""The hook health record: one machine-local row per hook handler failure.

Hooks are fail-open, so a handler that raises or a daemon call that fails
must not break the host session; the failure is kept here instead of being
lost. Rows append to ``.ea/local/hooks/health.jsonl`` in the main checkout's
tree, where every worktree's hooks and the status line meet. Every row still
in the record is unacknowledged: an operator acknowledges the failures by
removing the record once they have read it.

The module imports only the standard library and orjson because every hook
process loads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import orjson

#: The error code of a handler that raised.
HOOK_RAISED: Final = "hook_raised"

#: The error code of a daemon call a handler made that failed.
DAEMON_CALL_FAILED: Final = "daemon_call_failed"

#: Where the record lives, relative to a tree's ``.ea`` directory.
HEALTH_RECORD: Final = Path("local") / "hooks" / "health.jsonl"

#: The longest error detail a row keeps, so one runaway repr cannot bloat the record.
_DETAIL_CHARS: Final = 500


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


def record_failures(ea_dir: Path, failures: list[HookFailure]) -> int:
    """Append one health row per failure to the record under *ea_dir*.

    Each row is written by one append, so hook processes running side by
    side do not interleave their rows.

    Args:
        ea_dir: The tree's ``.ea`` directory.
        failures: The failures to record, in the order they happened.

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
    rows = b"".join(
        orjson.dumps(
            {
                "at": failure.at.isoformat(),
                "event": failure.event,
                "hook": failure.hook,
                "code": failure.code,
                "error": failure.error,
                "detail": failure.detail[:_DETAIL_CHARS],
            }
        )
        + b"\n"
        for failure in failures
    )
    with path.open("ab") as handle:
        handle.write(rows)
    return len(failures)


def unacknowledged_count(ea_dir: Path) -> int:
    """Return how many failures the record under *ea_dir* holds.

    Args:
        ea_dir: The tree's ``.ea`` directory.

    Returns:
        The number of non-blank rows; ``0`` when there is no record.

    Raises:
        OSError: The record exists but cannot be read.
    """
    try:
        with (ea_dir / HEALTH_RECORD).open("rb") as handle:
            return sum(1 for line in handle if line.strip())
    except FileNotFoundError:
        return 0


__all__ = [
    "DAEMON_CALL_FAILED",
    "HEALTH_RECORD",
    "HOOK_RAISED",
    "HookFailure",
    "failure_line",
    "record_failures",
    "unacknowledged_count",
]
