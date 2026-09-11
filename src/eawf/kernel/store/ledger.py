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
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for

logger = logging.getLogger(__name__)

LEDGER_SCHEMA_VERSION: Final = "2.0"


class LedgerError(RuntimeError):
    """A ledger operation was refused."""


class LedgerAppendOnlyError(LedgerError):
    """A write would have changed bytes the ledger already committed."""


class LedgerTornTailError(LedgerError):
    """The ledger ends mid-line, so it cannot be read until it is repaired."""


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


def read_ledger_records(path: Path) -> tuple[LedgerRecord, ...]:
    """Read every record of the ledger at *path*, in file order.

    Args:
        path: The ledger file. A missing file reads as empty.

    Returns:
        The records, in the order they were appended.

    Raises:
        LedgerTornTailError: The file ends mid-line.
        ValidationError: A line does not satisfy :class:`LedgerRecord`.
    """
    if not path.exists():
        return ()
    lines = split_ledger_lines(path.read_bytes())
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
    return append_json_line(path, render_ledger_line(record), timeout=timeout)


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
    content = path.read_bytes()
    if not content or content.endswith(b"\n"):
        return 0
    kept = content.rpartition(b"\n")[0]
    if kept:
        kept += b"\n"
    _atomic_write(path, kept)
    dropped = len(content) - len(kept)
    logger.warning(f"truncate_torn_tail path={path} dropped_bytes={dropped}")
    return dropped


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
    "LedgerAppendOnlyError",
    "LedgerError",
    "LedgerRecord",
    "LedgerTornTailError",
    "append_correction",
    "append_ledger_record",
    "content_digest",
    "effective_records",
    "guarded_ledger_write",
    "line_digest",
    "read_ledger_records",
    "render_ledger_line",
    "split_ledger_lines",
    "truncate_torn_tail",
    "verify_append_only",
]
