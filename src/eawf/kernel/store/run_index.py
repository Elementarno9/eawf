"""Reading a ledger again by validating only the lines appended since the last read.

The daemon re-reads the run ledger on every stall sweep and every
liveness pass, and the file only grows: a full read validates every line
the ledger has ever held to learn about the handful appended since. This
module keeps, per process, the records already validated and the byte
offset of the live file they end at, so a re-read validates the new
lines alone.

The index lives in process memory rather than on disk. A file-backed
cache would itself be untyped input that has to be validated on load,
which costs what it saves, and the reader that re-reads is the
long-running daemon, whose memory outlives every sweep. That memory is
several times the ledger's bytes, so only the few ledgers read most
recently are kept, and a ledger whose file is gone is dropped: a daemon
re-reads one run ledger per tree it serves, while a selected generation
that moved on leaves its old ledger behind for good.

A ledger is append-only, and every sanctioned rewrite -- a seal, a torn
tail repair, a leak scrub of unpublished lines -- replaces the file
through a rename, which gives it a new inode. So the index is reused only
while the live file is the same inode, at least as long as the offset,
and still holds the first and the last line already read at their old
places, and while the sealed snapshot is the same file; anything else
rebuilds it from the whole ledger. The checks run on bytes read through
one open file, so a writer appending meanwhile is either seen or not,
never half-trusted.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerSnapshotError,
    compose_ledger_content,
    ledger_snapshot,
    snapshot_digest_mismatch,
    split_ledger_lines,
)

#: The (dev, inode) a missing live file stands at.
_MISSING: tuple[int, int] = (-1, -1)


@dataclass(frozen=True, slots=True)
class _LedgerIndex:
    """What one read of a ledger left behind for the next.

    Attributes:
        snapshot: The sealed snapshot's name, size and modification time,
            ``None`` when the ledger had none.
        inode: The live file's (dev, inode), :data:`_MISSING` when absent.
        offset: Bytes of the live file read, always ending on a newline.
        head: The live file's first line read, with its newline.
        tail: The live file's last line read, with its newline.
        records: Every record the ledger held up to *offset*.
    """

    snapshot: tuple[str, int, int] | None
    inode: tuple[int, int]
    offset: int
    head: bytes
    tail: bytes
    records: tuple[LedgerRecord, ...]


#: How many ledgers keep an index: a handful of trees, one run ledger each.
_CAPACITY: Final = 4

#: Each ledger's index, keyed by its live path, least recently read first.
#: An entry is replaced whole, never mutated, so two threads reading at once
#: at worst both rebuild; the lock only keeps the order and the bound whole.
_INDEXES: OrderedDict[Path, _LedgerIndex] = OrderedDict()
_INDEXES_LOCK: Final = threading.Lock()


def indexed_ledger_records(path: Path) -> tuple[LedgerRecord, ...]:
    """Read every record of the ledger at *path*, validating only lines new since the last read.

    Returns exactly what :func:`~eawf.kernel.store.ledger.read_ledger_records`
    returns for the same bytes.

    Args:
        path: The live ledger file. A missing file reads as empty.

    Returns:
        The records, in the order they were appended.

    Raises:
        LedgerTornTailError: The file ends mid-line; the index is left
            where it was, so the next read retries the tail.
        LedgerSnapshotError: The snapshot does not match its digest, or
            the ledger has more than one.
        ValidationError: A line does not satisfy :class:`LedgerRecord`.
    """
    snapshot = ledger_snapshot(path)
    stamp = _snapshot_stamp(snapshot)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        with _INDEXES_LOCK:
            _INDEXES.pop(path, None)
        return _rebuild(path, snapshot, stamp, inode=_MISSING, live=b"").records
    with handle:
        stat = os.fstat(handle.fileno())
        inode = (stat.st_dev, stat.st_ino)
        with _INDEXES_LOCK:
            cached = _INDEXES.get(path)
        if cached is not None and cached.snapshot == stamp and cached.inode == inode:
            appended = _appended(handle, cached, size=stat.st_size)
            if appended is not None:
                return _store(path, _extend(cached, appended)).records
        handle.seek(0)
        live = handle.read()
    return _store(path, _rebuild(path, snapshot, stamp, inode=inode, live=live)).records


def _store(path: Path, index: _LedgerIndex) -> _LedgerIndex:
    """Keep *index* as the most recently read, dropping the gone and the least recent."""
    with _INDEXES_LOCK:
        _INDEXES[path] = index
        _INDEXES.move_to_end(path)
        for gone in [held for held in _INDEXES if held != path and not held.exists()]:
            del _INDEXES[gone]
        while len(_INDEXES) > _CAPACITY:
            _INDEXES.popitem(last=False)
    return index


def _snapshot_stamp(snapshot: Path | None) -> tuple[str, int, int] | None:
    """Return the name, size and modification time of *snapshot*, if any."""
    if snapshot is None:
        return None
    sealed = snapshot.stat()
    return (snapshot.name, sealed.st_size, sealed.st_mtime_ns)


def _appended(handle: BinaryIO, cached: _LedgerIndex, *, size: int) -> bytes | None:
    """Return the live bytes past *cached*'s offset, ``None`` when the prefix it read moved.

    Args:
        handle: The live file, open for binary reading.
        cached: The index of the last read.
        size: The open file's size.
    """
    if size < cached.offset:
        return None
    handle.seek(0)
    if handle.read(len(cached.head)) != cached.head:
        return None
    handle.seek(cached.offset - len(cached.tail))
    if handle.read(len(cached.tail)) != cached.tail:
        return None
    return handle.read()


def _extend(cached: _LedgerIndex, appended: bytes) -> _LedgerIndex:
    """Validate the lines *appended* past *cached* and return the longer index."""
    if not appended:
        return cached
    lines = split_ledger_lines(appended)
    added = tuple(LedgerRecord.model_validate_json(line) for line in lines)
    head = cached.head or (lines[0].encode("utf-8") + b"\n")
    return _LedgerIndex(
        snapshot=cached.snapshot,
        inode=cached.inode,
        offset=cached.offset + len(appended),
        head=head,
        tail=lines[-1].encode("utf-8") + b"\n",
        records=cached.records + added,
    )


def _rebuild(
    path: Path,
    snapshot: Path | None,
    stamp: tuple[str, int, int] | None,
    *,
    inode: tuple[int, int],
    live: bytes,
) -> _LedgerIndex:
    """Validate the whole ledger, its snapshot first, and return its index."""
    content = live
    if snapshot is not None:
        sealed = snapshot.read_bytes()
        reason = snapshot_digest_mismatch(snapshot.name, sealed)
        if reason is not None:
            raise LedgerSnapshotError(f"{reason}; refusing to read ledger {path.name}")
        content = compose_ledger_content(sealed, live)
    records = tuple(LedgerRecord.model_validate_json(line) for line in split_ledger_lines(content))
    live_lines = live.split(b"\n")[:-1]
    return _LedgerIndex(
        snapshot=stamp,
        inode=inode,
        offset=len(live),
        head=live_lines[0] + b"\n" if live_lines else b"",
        tail=live_lines[-1] + b"\n" if live_lines else b"",
        records=records,
    )


__all__ = ["indexed_ledger_records"]
