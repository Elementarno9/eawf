"""The derived offset index, and the guarantee that deleting it is safe.

The index says where each ledger line starts, how long it is, and whether
a later line supersedes it. It is a pure function of the ledger's bytes:
nothing is read from the clock, the filesystem or the process, and the
serialization is sorted and separator-pinned. Deleting the whole index
directory and regenerating therefore reproduces the previous bytes
exactly, which is what makes the tier disposable and keeps it out of
version control.

An index is only defined over a complete ledger. A torn tail is repaired
first (:func:`~eawf.kernel.store.ledger.truncate_torn_tail`); indexing
around one would publish offsets for bytes that recovery is about to
drop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.store.ledger import (
    LedgerRecord,
    content_digest,
    line_digest,
    split_ledger_lines,
)
from eawf.kernel.store.paths import index_path, ledger_path
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, Epoch2Collection

logger = logging.getLogger(__name__)

INDEX_SCHEMA_VERSION: Final = "1.0"


class LedgerIndexEntry(BaseModel):
    """Where one ledger line sits, and whether it still stands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    offset: Annotated[int, Field(ge=0)]
    length: Annotated[int, Field(gt=0)]
    digest: Sha256DigestStr
    superseded: bool


class LedgerIndex(BaseModel):
    """The offset index of one ledger, in file order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = INDEX_SCHEMA_VERSION
    collection: Epoch2Collection
    line_count: Annotated[int, Field(ge=0)]
    ledger_digest: Sha256DigestStr
    entries: tuple[LedgerIndexEntry, ...] = ()


def build_ledger_index(collection: Epoch2Collection, content: bytes) -> LedgerIndex:
    """Build the index of *collection* from the ledger's whole *content*.

    Args:
        collection: The ledger collection being indexed.
        content: Every byte of the ledger file.

    Returns:
        The index, whose entries are in file order.

    Raises:
        LedgerTornTailError: The content ends mid-line.
        ValidationError: A line does not satisfy
            :class:`~eawf.kernel.store.ledger.LedgerRecord`.
    """
    lines = split_ledger_lines(content)
    records = [LedgerRecord.model_validate_json(line) for line in lines]
    digests = [line_digest(line) for line in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}

    entries: list[LedgerIndexEntry] = []
    offset = 0
    for line, record, digest in zip(lines, records, digests, strict=True):
        length = len(line.encode("utf-8"))
        entries.append(
            LedgerIndexEntry(
                record_key=record.record_key,
                offset=offset,
                length=length,
                digest=digest,
                superseded=digest in superseded,
            )
        )
        offset += length + 1
    return LedgerIndex(
        collection=collection,
        line_count=len(entries),
        ledger_digest=content_digest(content),
        entries=tuple(entries),
    )


def render_index(index: LedgerIndex) -> bytes:
    """Return the one byte string an index serializes to.

    Args:
        index: The index to render.

    Returns:
        UTF-8 bytes with sorted keys, no insignificant whitespace and one
        trailing newline, so two equal indexes are byte-identical.
    """
    payload = json.dumps(
        index.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{payload}\n".encode()


def write_ledger_index(state_path: Path, collection: Epoch2Collection) -> LedgerIndex:
    """Rebuild and write the derived index of one ledger.

    Args:
        state_path: Path to the tree's ``state.json``.
        collection: The ledger collection to index.

    Returns:
        The index that was written.

    Raises:
        ValueError: The collection is not declared at the ledger tier.
        LedgerTornTailError: The ledger ends mid-line.
    """
    ledger = ledger_path(state_path, collection)
    content = ledger.read_bytes() if ledger.exists() else b""
    index = build_ledger_index(collection, content)
    target = index_path(state_path, collection)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(render_index(index))
    return index


def regenerate_indexes(state_path: Path) -> tuple[Epoch2Collection, ...]:
    """Rebuild the index of every ledger the tree holds.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        The collections that were indexed, in table order.

    Raises:
        LedgerTornTailError: A ledger ends mid-line.
    """
    written: list[Epoch2Collection] = []
    for collection in LEDGER_COLLECTIONS:
        if not ledger_path(state_path, collection).exists():
            continue
        write_ledger_index(state_path, collection)
        written.append(collection)
    logger.info(f"regenerate_indexes state_path={state_path} indexed={len(written)}")
    return tuple(written)


__all__ = [
    "INDEX_SCHEMA_VERSION",
    "LedgerIndex",
    "LedgerIndexEntry",
    "build_ledger_index",
    "regenerate_indexes",
    "render_index",
    "write_ledger_index",
]
