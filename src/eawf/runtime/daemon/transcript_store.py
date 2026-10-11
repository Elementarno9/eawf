"""Keep the whole text of a dispatched Run's messages in a machine-local store.

A message on the Run's stream holds at most a 500-character summary, because the
stream is a ledger every reader folds. What the worker actually said is kept here
instead: one JSONL file per Run under the selected generation's ``local/`` directory,
which the commit policy keeps out of version control, so no clone ever carries it.
Each line of a message is scanned on its way in and a line the state-leak scan flags
is replaced whole by :data:`~eawf.kernel.runtime.content.WITHHELD_LINE`, so the store
never holds a path, an address or a token shape either.

The store is a cache of words, not a record: the daemon prunes every file older than
``runtime.transcript.retention_days`` when it starts, and a reader asking for a
message whose file is gone is told it was pruned rather than that it never existed.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.content import (
    CONTENT_CHAR_LIMIT,
    CONTENT_LINE_CHARS,
    CONTENT_LINE_LIMIT,
    WITHHELD_LINE,
    ResolvedContent,
)
from eawf.kernel.runtime.provider import ArtifactUrn
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext

logger = logging.getLogger(__name__)

#: The reference prefix every kept message is named under.
TRANSCRIPT_REF_PREFIX: Final = "artifact://transcript/"

#: The directory, inside a generation's local tier, holding one file per Run.
TRANSCRIPT_DIRNAME: Final = "transcripts"

#: The leaf naming how many days a Run's file outlives its last write.
RETENTION_LEAF: Final = "runtime.transcript.retention_days"

#: The retention a missing or unusable leaf falls back to.
DEFAULT_RETENTION_DAYS: Final = 30

#: The fewest and the most days the leaf may name.
RETENTION_DAYS_MIN: Final = 1
RETENTION_DAYS_MAX: Final = 365

#: How many Runs' kept references are remembered at once; a Run past that is read
#: again from the top on its next message.
_REMEMBERED_RUNS: Final = 64

_StoredLine = Annotated[str, StringConstraints(strict=True)]


class TranscriptEntry(BaseModel):
    """One message as a Run's transcript file holds it.

    Attributes:
        ref: The reference the message's stream line names it by.
        lines: Every line of the message, each one the scan flagged replaced by
            :data:`WITHHELD_LINE`.
        withheld_lines: How many lines were replaced.
        recorded_at: When the daemon kept it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ArtifactUrn
    lines: tuple[_StoredLine, ...] = Field(min_length=1)
    withheld_lines: StrictNonNegativeInt
    recorded_at: UtcDatetime


@dataclass(frozen=True, slots=True)
class TranscriptRead:
    """What one transcript reference resolved to.

    Attributes:
        content: The kept message, cut to the bounds a reader receives; ``None`` when
            the Run's file holds no such message, or the file was pruned.
        pruned: Whether the Run's file is gone, so the message once kept no longer is.
    """

    content: ResolvedContent | None
    pruned: bool


def _run_segment(urn: QualifiedUrn) -> str:
    return urn.entity_key.lower()


def transcript_ref(urn: QualifiedUrn, event_ref: str) -> str:
    """Return the reference a message of *urn* is kept under.

    Args:
        urn: The Run that said the message.
        event_ref: The id of the message's stream line, the same on every replay of
            the turn, so a replay names the message it already kept.

    Returns:
        ``artifact://transcript/<run>/<event>``, lower-cased to the reference grammar.
    """
    return f"{TRANSCRIPT_REF_PREFIX}{_run_segment(urn)}/{event_ref.lower()}"


def transcript_dir(context: Epoch2RootContext) -> Path:
    """Return the selected generation's transcript directory (not created here).

    Raises:
        NativeAuthorityRequiredError: The tree is not native.
        MigrationDualAuthorityError: The selection is unreadable or disagrees.
        UndeclaredPathError: The commit policy does not declare the directory.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None, "an epoch-2 answer always carries its target"
    assert authority.generation_id is not None, "an epoch-2 answer always names a generation"
    generation = authority.target.generation_path(authority.generation_id)
    return context.declared_path(generation / LOCAL_DIRNAME / TRANSCRIPT_DIRNAME)


def _run_file(directory: Path, urn: QualifiedUrn) -> Path:
    return directory / f"{_run_segment(urn)}.jsonl"


def redacted_lines(text: str) -> tuple[tuple[str, ...], int]:
    """Return *text* as the store keeps it: its lines, each flagged one withheld.

    Args:
        text: The whole message.

    Returns:
        The lines with trailing blanks cut and every line the state-leak scan flags
        replaced by :data:`WITHHELD_LINE`, and how many were replaced.
    """
    allowed = default_allowed_emails()
    kept: list[str] = []
    withheld = 0
    for raw in text.strip().splitlines():
        if scan_state_leaks(raw, allowed_emails=allowed):
            kept.append(WITHHELD_LINE)
            withheld += 1
        else:
            kept.append(raw.rstrip())
    return tuple(kept), withheld


def _parsed(lines: Iterable[bytes], path: Path) -> Iterator[TranscriptEntry]:
    """Yield the entries *lines* hold, skipping a torn line or one that is not UTF-8."""
    for line in lines:
        try:
            yield TranscriptEntry.model_validate_json(line)
        except ValidationError:
            logger.warning(f"transcript_store skipped an unreadable line file={path.name}")


def _entries(path: Path) -> dict[str, TranscriptEntry]:
    """Return the entries of one Run's file by reference, skipping an unreadable line."""
    with path.open("rb") as handle:
        return {entry.ref: entry for entry in _parsed(handle, path)}


@dataclass(slots=True)
class _ReadSoFar:
    """How much of one Run's file has been read, and the references it held.

    Attributes:
        inode: The file the offset is into, so a pruned and rewritten file is
            read again rather than taken for the old one.
        offset: The byte just past the last whole line read.
        refs: Every reference read up to there.
    """

    inode: int
    offset: int
    refs: set[str]


class _KeptRefs:
    """The references each Run's file holds, each byte of the file read once.

    A turn keeps its messages one at a time, and the replay check each one makes
    would otherwise read the whole file again, so keeping a turn's messages cost
    the square of their count. Only the bytes past what was last read are read.
    """

    def __init__(self) -> None:
        """Remember nothing yet."""
        self.lock = threading.Lock()
        self._read: OrderedDict[Path, _ReadSoFar] = OrderedDict()

    def caught_up(self, path: Path) -> _ReadSoFar:
        """Return *path*'s references, reading only what was appended since the last read.

        Call it holding :attr:`lock`.

        Raises:
            OSError: The file could not be read.
        """
        size, inode = 0, 0
        if path.is_file():
            stat = path.stat()
            size, inode = stat.st_size, stat.st_ino
        read = self._read.pop(path, None)
        if read is None or read.inode != inode or read.offset > size:
            read = _ReadSoFar(inode=inode, offset=0, refs=set())
        if size > read.offset:
            with path.open("rb") as handle:
                handle.seek(read.offset)
                tail = handle.read(size - read.offset)
            whole = tail[: tail.rfind(b"\n") + 1]
            read.refs.update(entry.ref for entry in _parsed(whole.splitlines(), path))
            read.offset += len(whole)
        self._read[path] = read
        while len(self._read) > _REMEMBERED_RUNS:
            self._read.popitem(last=False)
        return read


_KEPT: Final = _KeptRefs()


def keep_message(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    event_ref: str,
    text: str,
    now: datetime,
) -> str | None:
    """Keep the whole of one message in its Run's file and return its reference.

    Args:
        context: The tree the Run lives in.
        urn: The Run that said the message.
        event_ref: The id of the message's stream line.
        text: The whole message, as the worker said it; it is redacted here.
        now: The daemon's recording clock.

    Returns:
        The message's reference, or ``None`` for a message with no words. A replay of
        a kept message names the line already standing and writes nothing.

    Raises:
        OSError: The file could not be written.
    """
    lines, withheld = redacted_lines(text)
    if not lines:
        return None
    ref = transcript_ref(urn, event_ref)
    directory = transcript_dir(context)
    path = _run_file(directory, urn)
    entry = TranscriptEntry(ref=ref, lines=lines, withheld_lines=withheld, recorded_at=now)
    with _KEPT.lock:
        read = _KEPT.caught_up(path)
        if ref in read.refs:
            return ref
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            start = handle.tell()
            handle.write(f"{entry.model_dump_json()}\n".encode())
            end = handle.tell()
        # Taken as read only when nothing else was appended in between; otherwise
        # the next catch-up reads this line along with the other writer's.
        inode = path.stat().st_ino
        if start == read.offset and read.inode in {0, inode}:
            read.inode = inode
            read.offset = end
            read.refs.add(ref)
    logger.info(f"keep_message run={urn.entity_key} lines={len(lines)} withheld={withheld}")
    return ref


def _bounded(ref: str, entry: TranscriptEntry) -> ResolvedContent:
    """Return *entry* cut to what one resolved content may hold.

    A line longer than a content line is folded into consecutive pieces rather than
    cut, so a long paragraph still reads whole up to the content's limits; the source
    count is the count of pieces, so a reader can state how many it is not showing.
    """
    pieces = [
        line[start : start + CONTENT_LINE_CHARS]
        for line in entry.lines
        for start in range(0, max(len(line), 1), CONTENT_LINE_CHARS)
    ]
    kept: list[str] = []
    spent = 0
    for piece in pieces[:CONTENT_LINE_LIMIT]:
        if spent + len(piece) > CONTENT_CHAR_LIMIT:
            break
        kept.append(piece)
        spent += len(piece)
    return ResolvedContent(
        ref=ref,
        lines=tuple(kept),
        total_lines=len(pieces),
        withheld_lines=sum(1 for piece in kept if piece == WITHHELD_LINE),
    )


def read_message(context: Epoch2RootContext, urn: QualifiedUrn, ref: str) -> TranscriptRead:
    """Resolve one transcript reference of *urn* from the local store.

    Args:
        context: The tree the Run lives in.
        urn: The Run whose transcript names the reference.
        ref: A reference under :data:`TRANSCRIPT_REF_PREFIX`.

    Returns:
        The kept message; no content when the reference names another Run or a
        message the file does not hold; ``pruned`` when the Run's file is gone.
    """
    run_segment, _, _event = ref.removeprefix(TRANSCRIPT_REF_PREFIX).partition("/")
    if run_segment != _run_segment(urn):
        return TranscriptRead(content=None, pruned=False)
    path = _run_file(transcript_dir(context), urn)
    if not path.is_file():
        return TranscriptRead(content=None, pruned=True)
    entry = _entries(path).get(ref)
    return TranscriptRead(content=None if entry is None else _bounded(ref, entry), pruned=False)


def resolve_retention_days(repo_root: Path) -> int:
    """Return the days ``runtime.transcript.retention_days`` resolves to for *repo_root*.

    Read best-effort at boot: an unreadable config, or a value that is not a whole
    number within the leaf's range, leaves the store on the default rather than
    failing the start.

    Args:
        repo_root: The repository whose layered config is consulted.

    Returns:
        The configured days, or :data:`DEFAULT_RETENTION_DAYS`.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config

    try:
        merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
        raw = get_dotted(merged, RETENTION_LEAF)
    except KeyError:
        return DEFAULT_RETENTION_DAYS
    except Exception as error:
        logger.warning(f"resolve_retention_days unreadable err={error!r}; default")
        return DEFAULT_RETENTION_DAYS
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not RETENTION_DAYS_MIN <= raw <= RETENTION_DAYS_MAX
    ):
        logger.warning(f"resolve_retention_days unusable raw={raw!r}; default")
        return DEFAULT_RETENTION_DAYS
    return raw


def prune_transcripts(directory: Path, *, retention_days: int, now: datetime) -> int:
    """Remove every Run's file in *directory* last written more than *retention_days* ago.

    Only the Run files the store writes are considered; anything else in the directory
    stays.

    Args:
        directory: A generation's transcript directory.
        retention_days: How many days a file outlives its last write.
        now: The daemon's clock.

    Returns:
        How many files were removed; zero when the directory does not exist.

    Raises:
        OSError: The directory could not be listed or a file could not be removed.
    """
    if not directory.is_dir():
        return 0
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    removed = 0
    for entry in directory.glob("*.jsonl"):
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            entry.unlink(missing_ok=True)
            removed += 1
    return removed


def prune_tree_transcripts(tree_root: Path, *, now: datetime) -> int:
    """Prune the selected generation's transcripts of one tree under its own retention.

    Args:
        tree_root: The tree's ``.ea`` directory; a tree that is not native has no
            transcript store and is left alone.
        now: The daemon's clock.

    Returns:
        How many files were removed.

    Raises:
        OSError: The directory could not be listed or a file could not be removed.
    """
    authority = resolve_authority(tree_root)
    if authority.gap is not None or authority.target is None or authority.generation_id is None:
        return 0
    directory = (
        authority.target.generation_path(authority.generation_id)
        / LOCAL_DIRNAME
        / TRANSCRIPT_DIRNAME
    )
    retention = resolve_retention_days(Path(tree_root).parent)
    removed = prune_transcripts(directory, retention_days=retention, now=now)
    logger.info(
        f"prune_tree_transcripts tree={Path(tree_root).name} "
        f"retention_days={retention} removed={removed}"
    )
    return removed


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_MAX",
    "RETENTION_DAYS_MIN",
    "RETENTION_LEAF",
    "TRANSCRIPT_DIRNAME",
    "TRANSCRIPT_REF_PREFIX",
    "TranscriptEntry",
    "TranscriptRead",
    "keep_message",
    "prune_transcripts",
    "prune_tree_transcripts",
    "read_message",
    "redacted_lines",
    "resolve_retention_days",
    "transcript_dir",
    "transcript_ref",
]
