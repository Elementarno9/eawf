"""The append-only ledger: what a line is, and why one is never edited.

A ledger line is written once. It is never rewritten, reordered or
removed, because every derived index, every digest taken over the file
and every reader that remembers a byte offset assumes the prefix it
already read cannot change underneath it. A wrong line is corrected by
appending a line that names the digest of the line it supersedes, so the
correction and the thing it corrects are both still readable.

The guard is structural rather than advisory.
:func:`verify_append_only` refuses any content that is not an extension
of what the file already holds, which catches a rewrite, a reorder and a
removal with one check, and :func:`guarded_ledger_write` is the only
sanctioned way to replace a ledger's bytes wholesale.

A ledger may be sealed once (:func:`seal_ledger_prefix`): a leading run of
its lines moves, byte for byte, into ``snapshots/<stem>.<sha256>.jsonl``
beside it, and the live file keeps only what follows. The ledger a reader
sees is the snapshot's bytes followed by the live file's, so sealing
changes no record, no order and no digest; it only stops every later
commit of the live file from re-storing the sealed bytes. The snapshot is
named by its own sha256, and a snapshot whose bytes no longer hash to
that name is refused rather than read.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.types import Sha256DigestStr, UtcDatetime
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

logger = logging.getLogger(__name__)

LEDGER_SCHEMA_VERSION: Final = "2.0"


class LedgerError(RuntimeError):
    """A ledger operation was refused."""


class LedgerAppendOnlyError(LedgerError):
    """A write would have changed bytes the ledger already committed."""


class LedgerTornTailError(LedgerError):
    """The ledger ends mid-line, so it cannot be read until it is repaired."""


class LedgerSnapshotError(LedgerError):
    """A ledger's sealed snapshot cannot be trusted or cannot be written."""


#: The directory beside a ledger that holds its sealed snapshot.
SNAPSHOT_DIRNAME: Final = "snapshots"


class LedgerRecord(BaseModel):
    """One line of an epoch-2 ledger.

    Identity is ``(collection, record_key)``: the ledger may hold several
    lines for one key, and a later line supersedes an earlier one by
    naming its digest rather than by replacing it in place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = LEDGER_SCHEMA_VERSION
    collection: Epoch2Collection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    status: Annotated[str, Field(min_length=1, max_length=64)]
    recorded_at: UtcDatetime
    supersedes: Sha256DigestStr | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def render_ledger_line(record: LedgerRecord) -> str:
    """Return the exact text one *record* occupies, without its newline."""
    return record.model_dump_json()


def line_digest(line: str) -> str:
    """Return the ``sha256:``-prefixed digest of one ledger line.

    Args:
        line: The line text, without its trailing newline.

    Returns:
        The digest a correction names to supersede this line.
    """
    return f"sha256:{hashlib.sha256(line.encode('utf-8')).hexdigest()}"


def content_digest(content: bytes) -> str:
    """Return the ``sha256:``-prefixed digest of a whole ledger file."""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def split_ledger_lines(content: bytes) -> tuple[str, ...]:
    """Split a ledger's bytes into its complete lines.

    Args:
        content: The whole file as read.

    Returns:
        Every line, in file order, without trailing newlines.

    Raises:
        LedgerTornTailError: The content does not end with a newline, so
            the last line was only partly written and the file has to be
            repaired before it can be read.
    """
    if not content:
        return ()
    if not content.endswith(b"\n"):
        raise LedgerTornTailError("ledger ends mid-line; repair the torn tail before reading")
    return tuple(content.decode("utf-8").split("\n")[:-1])


def snapshot_name(stem: str, content: bytes) -> str:
    """Return the file name a snapshot of *content* for the ledger *stem* takes.

    Args:
        stem: The ledger's file stem, such as ``task``.
        content: The sealed bytes.

    Returns:
        ``<stem>.<sha256 hex>.jsonl``.
    """
    return f"{stem}.{hashlib.sha256(content).hexdigest()}.jsonl"


def snapshot_digest_mismatch(name: str, content: bytes) -> str | None:
    """Explain why *content* is not the snapshot *name* promises, if it is not.

    Args:
        name: The snapshot's file name, ``<stem>.<sha256 hex>.jsonl``.
        content: The bytes the file holds.

    Returns:
        ``None`` when the bytes hash to the digest in the name, otherwise
        a one-line reason.
    """
    stem, _, rest = name.partition(".")
    named = rest.removesuffix(".jsonl")
    actual = hashlib.sha256(content).hexdigest()
    if not stem or len(named) != 64 or name != f"{stem}.{named}.jsonl":
        return f"snapshot {name!r} is not named <stem>.<sha256>.jsonl"
    if actual != named:
        return f"snapshot {name!r} hashes to sha256:{actual}, not the digest its name carries"
    return None


def ledger_snapshot(path: Path) -> Path | None:
    """Return the sealed snapshot of the ledger at *path*, ``None`` when it has none.

    Args:
        path: The live ledger file.

    Returns:
        The one ``snapshots/<stem>.<sha256>.jsonl`` beside the ledger.

    Raises:
        LedgerSnapshotError: More than one snapshot claims the ledger, so
            the order its sealed lines read in is undefined.
    """
    found = sorted((path.parent / SNAPSHOT_DIRNAME).glob(f"{path.stem}.*.jsonl"))
    if len(found) > 1:
        names = ", ".join(item.name for item in found)
        raise LedgerSnapshotError(f"ledger {path.name} has {len(found)} snapshots: {names}")
    return found[0] if found else None


def compose_ledger_content(snapshot: bytes, live: bytes) -> bytes:
    """Return the ledger a sealed *snapshot* and its *live* file make together.

    A seal writes the snapshot before it shortens the live file, so a
    crash between the two leaves a live file that still starts with the
    snapshot's bytes. That file is the whole ledger as it stood before the
    seal, and it is read alone rather than with the snapshot in front.

    Args:
        snapshot: The verified snapshot bytes; empty when there is none.
        live: The live file's bytes.

    Returns:
        The ledger's bytes in append order.
    """
    if not snapshot or live.startswith(snapshot):
        return live
    return snapshot + live


def read_ledger_content(path: Path) -> bytes:
    """Return every byte of the ledger at *path*: its snapshot, then its live file.

    Args:
        path: The live ledger file. A missing file reads as empty.

    Returns:
        The ledger's bytes in append order.

    Raises:
        LedgerSnapshotError: The snapshot's bytes do not hash to the digest
            its name carries, or the ledger has more than one snapshot.
    """
    live = path.read_bytes() if path.exists() else b""
    snapshot = ledger_snapshot(path)
    if snapshot is None:
        return live
    sealed = snapshot.read_bytes()
    reason = snapshot_digest_mismatch(snapshot.name, sealed)
    if reason is not None:
        raise LedgerSnapshotError(f"{reason}; refusing to read ledger {path.name}")
    return compose_ledger_content(sealed, live)


def read_ledger_records(path: Path) -> tuple[LedgerRecord, ...]:
    """Read every record of the ledger at *path*, in append order.

    A sealed snapshot's lines come first, then the live file's.

    Args:
        path: The ledger file. A missing file reads as empty.

    Returns:
        The records, in the order they were appended.

    Raises:
        LedgerTornTailError: The file ends mid-line.
        LedgerSnapshotError: The snapshot does not match its digest.
        ValidationError: A line does not satisfy :class:`LedgerRecord`.
    """
    lines = split_ledger_lines(read_ledger_content(path))
    return tuple(LedgerRecord.model_validate_json(line) for line in lines)


def append_ledger_record(path: Path, record: LedgerRecord, *, timeout: float = 5.0) -> int:
    """Append one *record* to the ledger at *path*.

    Args:
        path: The ledger file, created with its parent if missing.
        record: The record to commit.
        timeout: Seconds to wait for the sibling append lock.

    Returns:
        The byte offset the line was written at.

    Raises:
        ValueError: The record's collection is not declared at the ledger
            tier, so it has no append-only file to land in.
        StateConflict: The sibling lock could not be acquired.
    """
    if tier_for(record.collection) is not StorageTier.LEDGER:
        raise ValueError(
            f"{record.collection.value!r} is declared at the "
            f"{tier_for(record.collection).value} tier, so it has no ledger"
        )
    # The append lock and its CLI error type load only on a write; most
    # importers of this module only read.
    from eawf.kernel.store.append import append_json_line

    return append_json_line(path, render_ledger_line(record), timeout=timeout)


def append_ledger_record_once(path: Path, record: LedgerRecord) -> bool:
    """Append *record* unless the ledger already holds its exact line.

    A crash replay re-issues a line its journal says was committed without
    knowing whether the append itself reached the disk, so the replay has
    to be a no-op when it did. A line is recognised by its digest, which
    covers the timestamp and the payload, so a later line that merely
    reuses the key is still appended.

    Args:
        path: The ledger file, created with its parent if missing.
        record: The record the journal says was committed.

    Returns:
        ``True`` when the line was appended now, ``False`` when the ledger
        already held it.

    Raises:
        LedgerTornTailError: The ledger ends mid-line, so whether it holds
            the line cannot be read until the tail is repaired.
        ValueError: The record's collection has no ledger.
    """
    wanted = line_digest(render_ledger_line(record))
    held = {line_digest(render_ledger_line(item)) for item in read_ledger_records(path)}
    if wanted in held:
        return False
    append_ledger_record(path, record)
    return True


def append_correction(path: Path, record: LedgerRecord, *, timeout: float = 5.0) -> int:
    """Append a correction that supersedes an existing line.

    Args:
        path: The ledger file.
        record: The correcting record, whose ``supersedes`` names the
            digest of the line it replaces.
        timeout: Seconds to wait for the sibling append lock.

    Returns:
        The byte offset the correction was written at.

    Raises:
        LedgerError: The record names no superseded line, or names a
            digest the ledger does not hold -- both of which would leave
            a correction pointing at nothing.
    """
    if record.supersedes is None:
        raise LedgerError("a correction must name the digest of the line it supersedes")
    known = {line_digest(render_ledger_line(item)) for item in read_ledger_records(path)}
    if record.supersedes not in known:
        raise LedgerError(f"ledger holds no line with digest {record.supersedes}")
    return append_ledger_record(path, record, timeout=timeout)


def effective_records(records: tuple[LedgerRecord, ...]) -> tuple[LedgerRecord, ...]:
    """Return *records* with every superseded line dropped.

    Args:
        records: The ledger's lines, in file order.

    Returns:
        The lines no later line supersedes, in file order.
    """
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    return tuple(
        record for record in records if line_digest(render_ledger_line(record)) not in superseded
    )


#: Each ledger's latest rows, beside the size and modification time of the live file
#: and the name, size and modification time of its snapshot when they were read. A
#: ledger only grows, so unchanged stamps mean unchanged bytes, and a read of a
#: several-megabyte ledger is paid once per append rather than once per request.
_LATEST_ROWS: dict[Path, tuple[tuple[int | str, ...], Mapping[str, dict[str, Any]]]] = {}


def latest_rows(path: Path) -> Mapping[str, dict[str, Any]]:
    """Return the latest standing row filed under each key of the ledger at *path*.

    A row is a line whose payload names the line's own ``record_key`` as its ``key``:
    a record that compacted out of the document. A line filed there for another
    reason -- an imported legacy record, an acceptance-bundle revision, a notice --
    carries no such key and is passed over.

    Args:
        path: The ledger file. A missing file reads as empty.

    Returns:
        The payload of the last standing row of each key, keyed by that key.

    Raises:
        LedgerTornTailError: The file ends mid-line.
        LedgerSnapshotError: The snapshot does not match its digest.
        ValidationError: A line does not satisfy :class:`LedgerRecord`.
    """
    snapshot = ledger_snapshot(path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        if snapshot is None:
            return MappingProxyType({})
        stamp: tuple[int | str, ...] = (-1, -1)
    else:
        stamp = (stat.st_size, stat.st_mtime_ns)
    if snapshot is not None:
        sealed = snapshot.stat()
        stamp = (*stamp, snapshot.name, sealed.st_size, sealed.st_mtime_ns)
    cached = _LATEST_ROWS.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    rows = MappingProxyType(
        {
            record.record_key: record.payload
            for record in effective_records(read_ledger_records(path))
            if record.payload.get("key") == record.record_key
        }
    )
    _LATEST_ROWS[path] = (stamp, rows)
    return rows


def verify_append_only(before: bytes, after: bytes) -> None:
    """Refuse *after* unless it extends *before* byte for byte.

    Args:
        before: What the ledger holds now.
        after: What a writer proposes to leave behind.

    Raises:
        LedgerAppendOnlyError: The proposed content is shorter than the
            current content or diverges from it, which is a removal, a
            reorder or a rewrite of a committed line.
    """
    if len(after) < len(before):
        raise LedgerAppendOnlyError(
            f"proposed content drops {len(before) - len(after)} committed bytes"
        )
    if not after.startswith(before):
        prefix = os.path.commonprefix([before, after])
        raise LedgerAppendOnlyError(f"proposed content diverges at byte {len(prefix)}")


def guarded_ledger_write(path: Path, content: bytes) -> None:
    """Replace the ledger at *path* with *content*, refusing any non-extension.

    Args:
        path: The ledger file.
        content: The whole proposed file, which must extend what is
            already there.

    Raises:
        LedgerAppendOnlyError: The proposal rewrites, reorders or removes
            a committed line.
    """
    current = path.read_bytes() if path.exists() else b""
    verify_append_only(current, content)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content)


def seal_ledger_prefix(path: Path, cut: int, *, timeout: float = 5.0) -> Path:
    """Move the ledger's first *cut* bytes into a snapshot named by their sha256.

    The snapshot is written first and the live file shortened second, both
    atomically and under the ledger's append lock, so a crash between the
    two leaves a live file that still starts with the snapshot and reads
    as the unsealed ledger (:func:`compose_ledger_content`);
    :func:`finish_interrupted_seal` completes it.

    Args:
        path: The live ledger file.
        cut: The byte offset to seal through; it must end a line.
        timeout: Seconds to wait for the sibling append lock.

    Returns:
        The snapshot written.

    Raises:
        LedgerSnapshotError: The ledger already has a snapshot, or *cut*
            is not a line boundary inside the file.
        LedgerTornTailError: The file ends mid-line.
        StateConflict: The sibling lock could not be acquired.
    """
    from eawf.runtime.lock import portalock
    from eawf.surfaces.cli.errors import StateConflict

    try:
        with portalock.acquire(path, timeout=timeout):
            if ledger_snapshot(path) is not None:
                raise LedgerSnapshotError(f"ledger {path.name} is already sealed")
            live = path.read_bytes() if path.exists() else b""
            if not 0 < cut <= len(live) or live[cut - 1 : cut] != b"\n":
                raise LedgerSnapshotError(
                    f"cut {cut} is not a line boundary inside {path.name} ({len(live)} bytes)"
                )
            split_ledger_lines(live)
            sealed = live[:cut]
            snapshot = path.parent / SNAPSHOT_DIRNAME / snapshot_name(path.stem, sealed)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(snapshot, sealed)
            _atomic_write(path, live[cut:])
    except portalock.LockTimeout as exc:
        raise StateConflict(
            f"could not acquire append lock for {path}: {exc}", kind="LockConflict"
        ) from exc
    logger.info(f"seal_ledger_prefix path={path} sealed_bytes={cut} snapshot={snapshot.name}")
    return snapshot


def finish_interrupted_seal(path: Path) -> bool:
    """Shorten a live ledger a seal snapshotted but did not get to shorten.

    Args:
        path: The live ledger file.

    Returns:
        ``True`` when the live file still started with its snapshot and
        was shortened, ``False`` when there was nothing to finish.

    Raises:
        LedgerSnapshotError: The snapshot does not match its digest.
    """
    snapshot = ledger_snapshot(path)
    if snapshot is None or not path.exists():
        return False
    sealed = snapshot.read_bytes()
    reason = snapshot_digest_mismatch(snapshot.name, sealed)
    if reason is not None:
        raise LedgerSnapshotError(reason)
    live = path.read_bytes()
    if not live.startswith(sealed):
        return False
    _atomic_write(path, live[len(sealed) :])
    logger.warning(f"finish_interrupted_seal path={path} dropped_bytes={len(sealed)}")
    return True


def truncate_torn_tail(path: Path) -> int:
    """Drop a partly written trailing line so the ledger reads again.

    A line is appended with one write and one fsync, so an interrupted
    append leaves bytes with no terminating newline. Everything before
    the last newline is committed and is left exactly as it is.

    Args:
        path: The ledger file. A missing file is a no-op.

    Returns:
        How many bytes were dropped; ``0`` when the ledger was intact.
    """
    if not path.exists():
        return 0
    kept, torn = split_torn_tail(path.read_bytes())
    if not torn:
        return 0
    _atomic_write(path, kept)
    logger.warning(f"truncate_torn_tail path={path} dropped_bytes={len(torn)}")
    return len(torn)


def split_torn_tail(content: bytes) -> tuple[bytes, bytes]:
    """Split a ledger's bytes into its complete lines and its torn tail.

    Args:
        content: The whole file as read.

    Returns:
        ``(kept, torn)``: every byte up to and including the last newline,
        and whatever follows it. ``torn`` is empty when the ledger ends on
        a line boundary, which an empty ledger does.
    """
    cut = content.rfind(b"\n") + 1
    return content[:cut], content[cut:]


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace *path* with *content* via a fsynced tempfile and rename."""
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "SNAPSHOT_DIRNAME",
    "LedgerAppendOnlyError",
    "LedgerError",
    "LedgerRecord",
    "LedgerSnapshotError",
    "LedgerTornTailError",
    "append_correction",
    "append_ledger_record",
    "append_ledger_record_once",
    "compose_ledger_content",
    "content_digest",
    "effective_records",
    "finish_interrupted_seal",
    "guarded_ledger_write",
    "latest_rows",
    "ledger_snapshot",
    "line_digest",
    "read_ledger_content",
    "read_ledger_records",
    "render_ledger_line",
    "seal_ledger_prefix",
    "snapshot_digest_mismatch",
    "snapshot_name",
    "split_ledger_lines",
    "split_torn_tail",
    "truncate_torn_tail",
    "verify_append_only",
]
