"""Attribute runtime-dir entry churn to the daemon that caused it.

The test suite guards the operator's live runtime directory: a run must
never create, remove or replace an entry there. A live daemon that idles
out mid-run removes its own socket and PID file, and that looks exactly
like a suite write to anyone reading the directory alone. So the daemon
appends an intent record naming the entries it is about to touch BEFORE
it touches them, and the guard exempts a change only when such a record
covers it. Writing the record first means a guard that snapshots right
after the change always finds the record already on disk.

Each record carries the suite session tag the daemon inherited from the
process that spawned it. A daemon the test suite itself spawned into the
live directory therefore carries a tag and is never exempt: only a
daemon started outside any suite can explain away a change.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

#: Append-only intent ledger inside the runtime directory.
CHURN_LEDGER_NAME: Final[str] = "eawfd.churn.jsonl"

#: The single rotated generation kept beside :data:`CHURN_LEDGER_NAME`.
CHURN_LEDGER_ROTATED_NAME: Final[str] = "eawfd.churn.jsonl.1"

#: The ledger's own files are daemon bookkeeping, not churn to explain.
CHURN_LEDGER_NAMES: Final[frozenset[str]] = frozenset(
    {CHURN_LEDGER_NAME, CHURN_LEDGER_ROTATED_NAME}
)

#: Size at which the ledger rotates. A boot plus an exit record is about
#: 300 bytes, so this holds hundreds of daemon lifetimes, far more than
#: any guarded window spans.
CHURN_LEDGER_MAX_BYTES: Final[int] = 256 * 1024

#: Env var the test suite sets so every daemon it spawns is marked.
SUITE_SESSION_ENV: Final[str] = "EAWF_SUITE_SESSION"

#: How far before the guard's first snapshot a record still counts. A
#: booting daemon writes its record, then replays its WAL for seconds
#: before it binds the socket, so a snapshot taken mid-boot sees the
#: socket appear under a record written just before the window opened.
CHURN_LOOKBACK_NS: Final[int] = 60 * 1_000_000_000

#: Reported in place of an entry name when the directory itself changed
#: (created, removed, or an entry came and went between snapshots) with
#: no daemon record in the window to explain it.
DIRECTORY_CHANGE: Final[str] = "."


class ChurnOp(StrEnum):
    """Which daemon lifecycle step wrote a churn record.

    Attributes:
        BOOT: The daemon is about to write its PID file and bind.
        EXIT: The daemon is about to remove its socket and PID file.
    """

    BOOT = "boot"
    EXIT = "exit"


class ChurnRecord(BaseModel):
    """One intent line in the churn ledger.

    Attributes:
        pid: The daemon process that wrote the record.
        op: The lifecycle step about to run.
        at_ns: Wall-clock ``time.time_ns()`` when the record was written.
        suite_session: The inherited :data:`SUITE_SESSION_ENV` value, or
            ``None`` for a daemon started outside any test suite.
        touched: Runtime-dir entry names the step is about to create,
            replace or remove.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(gt=0)
    op: ChurnOp
    at_ns: int = Field(ge=0)
    suite_session: str | None
    touched: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class RuntimeDirSnapshot:
    """What a runtime directory held at one instant.

    Attributes:
        exists: Whether the directory existed.
        mtime_ns: Directory mtime, ``0`` when absent. It moves on every
            entry create, remove or rename, so it also witnesses an entry
            that came and went between two snapshots.
        captured_at_ns: Wall-clock ``time.time_ns()`` taken before the scan.
        entries: Top-level entry name to inode. An inode change catches an
            atomic replace that leaves the name set unchanged.
    """

    exists: bool
    mtime_ns: int
    captured_at_ns: int
    entries: Mapping[str, int]


def record_churn(runtime_dir: Path, *, op: ChurnOp, touched: tuple[str, ...]) -> None:
    """Append an intent record for entries the daemon is about to touch.

    Never raises on I/O: a daemon that cannot write its ledger must still
    boot and exit. The cost of a lost record is a red guard, which is the
    safe direction.

    Args:
        runtime_dir: The daemon's runtime directory.
        op: The lifecycle step about to run.
        touched: Entry names the step will create, replace or remove.
    """
    record = ChurnRecord(
        pid=os.getpid(),
        op=op,
        at_ns=time.time_ns(),
        suite_session=os.environ.get(SUITE_SESSION_ENV) or None,
        touched=touched,
    )
    ledger = runtime_dir / CHURN_LEDGER_NAME
    try:
        if ledger.exists() and ledger.stat().st_size > CHURN_LEDGER_MAX_BYTES:
            ledger.replace(runtime_dir / CHURN_LEDGER_ROTATED_NAME)
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(f"{record.model_dump_json()}\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        logger.warning(f"record_churn write-failed op={op.value} error={exc!s}")


def read_churn_records(runtime_dir: Path) -> tuple[ChurnRecord, ...]:
    """Return every readable record from both ledger generations.

    A line that does not validate (a torn tail from a killed writer) is
    skipped with a warning: the entries it named stay unexplained, which
    reds the guard rather than hiding a write.

    Args:
        runtime_dir: The runtime directory holding the ledger.

    Returns:
        Records in file order, rotated generation first; empty when no
        ledger exists.
    """
    records: list[ChurnRecord] = []
    for name in (CHURN_LEDGER_ROTATED_NAME, CHURN_LEDGER_NAME):
        try:
            text = (runtime_dir / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(ChurnRecord.model_validate_json(line))
            except ValidationError as exc:
                logger.warning(
                    f"read_churn_records skip-invalid ledger={name!r} errors={exc.error_count()}"
                )
    return tuple(records)


def snapshot_runtime_dir(path: Path) -> RuntimeDirSnapshot:
    """Capture the entry set and mtime of *path*.

    Args:
        path: The runtime directory to snapshot.

    Returns:
        The snapshot; an absent directory yields ``exists=False`` with no
        entries.
    """
    captured_at_ns = time.time_ns()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return RuntimeDirSnapshot(
            exists=False, mtime_ns=0, captured_at_ns=captured_at_ns, entries={}
        )
    entries: dict[str, int] = {}
    with os.scandir(path) as scan:
        for entry in scan:
            try:
                entries[entry.name] = entry.inode()
            except FileNotFoundError:
                continue
    return RuntimeDirSnapshot(
        exists=True,
        mtime_ns=stat.st_mtime_ns,
        captured_at_ns=captured_at_ns,
        entries=entries,
    )


def unattributed_changes(
    before: RuntimeDirSnapshot,
    after: RuntimeDirSnapshot,
    records: Iterable[ChurnRecord],
    *,
    lookback_ns: int = CHURN_LOOKBACK_NS,
) -> tuple[str, ...]:
    """Return the changes between two snapshots no daemon record explains.

    A record explains a change when it was written inside the window
    (from *lookback_ns* before *before* up to *after*), carries no suite
    session tag, and names the changed entry.

    Args:
        before: Snapshot taken when the guarded window opened.
        after: Snapshot taken when it closed.
        records: Ledger records, read after *after* was taken.
        lookback_ns: How far before *before* a record still counts.

    Returns:
        Sorted unexplained entry names, plus :data:`DIRECTORY_CHANGE` when
        the directory itself changed with no daemon record in the window.
        Empty means the guard passes.

    Raises:
        ValueError: *lookback_ns* is negative.
    """
    if lookback_ns < 0:
        raise ValueError(f"lookback_ns must be non-negative, got {lookback_ns!r}")
    window_start = before.captured_at_ns - lookback_ns
    daemon_records = [
        record
        for record in records
        if record.suite_session is None and window_start <= record.at_ns <= after.captured_at_ns
    ]
    explained = {name for record in daemon_records for name in record.touched}
    names = before.entries.keys() | after.entries.keys()
    changed = {name for name in names if before.entries.get(name) != after.entries.get(name)}
    unexplained = sorted(changed - explained - CHURN_LEDGER_NAMES)
    directory_moved = before.exists != after.exists or before.mtime_ns != after.mtime_ns
    if directory_moved and not daemon_records and not unexplained:
        unexplained.append(DIRECTORY_CHANGE)
    return tuple(unexplained)


__all__ = [
    "CHURN_LEDGER_MAX_BYTES",
    "CHURN_LEDGER_NAME",
    "CHURN_LEDGER_NAMES",
    "CHURN_LEDGER_ROTATED_NAME",
    "CHURN_LOOKBACK_NS",
    "DIRECTORY_CHANGE",
    "SUITE_SESSION_ENV",
    "ChurnOp",
    "ChurnRecord",
    "RuntimeDirSnapshot",
    "read_churn_records",
    "record_churn",
    "snapshot_runtime_dir",
    "unattributed_changes",
]
