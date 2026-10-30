"""A machine-local index of the transcript file each runtime session writes.

A Claude Code or Codex session writes its conversation to a file the runtime owns,
named by a session id that state never holds raw. This index ties each such file to
the session's digest, and to the Run the session served when that is known, so a
reader finds a Run's transcript without scanning the runtime's directories. It lives
under the tree's ``local/`` directory, which the commit policy keeps out of version
control, because a transcript path is a path on the operator's machine.

Rows are appended under the file's lock and never rewritten. A row equal to one the
index already holds is not appended again, because a session's hooks report the same
file at its start and at its end; a writer reads the index under the lock it appends
under. The daemon's back-fill reads the index once per pass, so a hook writing beside
it can still leave two equal rows, which a reader treats as one.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from eawf.kernel.state.epoch2.measurement import VendorSessionDigest, VendorSessionRef
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line, append_json_line_unless
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.runtime.runtimes.claude.transcript_counters import projects_root
from eawf.runtime.runtimes.host_transcript import HostHarness
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: The index file, inside the tree's ``local/`` directory.
TRANSCRIPT_INDEX_FILENAME: Final = "transcript-index.jsonl"

#: The harnesses whose transcript directories a back-fill scans.
_HARNESSES: Final[tuple[HostHarness, ...]] = ("claude-code", "codex")

#: Where a Claude Code session keeps its subagents' transcripts, and their name prefix.
_SUBAGENT_DIRNAME: Final = "subagents"
_SUBAGENT_PREFIX: Final = "agent-"

#: A Codex rollout file ends in its thread id, a 36-character UUID.
_CODEX_THREAD_ID_CHARS: Final = 36

_Text = Annotated[str, StringConstraints(strict=True, min_length=1)]


class TranscriptIndexRow(BaseModel):
    """One session's transcript file, as the index holds it.

    Attributes:
        runtime: The harness that wrote the file.
        vendor_session_digest: The session's id, hashed. A raw id is hashed on
            validation, so the raw id never reaches the file.
        transcript_path: The file on this machine.
        run_ref: The Run the session served; ``None`` for a host session no Run
            was resolved for.
        parent_session_digest: For a subagent, the hashed id of the session that
            spawned it; ``None`` otherwise.
        recorded_at: When the row was written.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: HostHarness
    vendor_session_digest: VendorSessionDigest
    transcript_path: _Text
    run_ref: _Text | None = None
    parent_session_digest: VendorSessionDigest | None = None
    recorded_at: UtcDatetime

    @field_validator("vendor_session_digest", "parent_session_digest", mode="before")
    @classmethod
    def _hash_raw_id(cls, value: object) -> object:
        """Hash a raw vendor session id; pass anything else to validation."""
        if isinstance(value, str) and value:
            return hash_vendor_session_id(value)
        return value


def transcript_index_path(tree_root: Path) -> Path:
    """Return the index file of the tree at *tree_root* (not created here).

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        ``<tree_root>/local/transcript-index.jsonl``.
    """
    return tree_root / LOCAL_DIRNAME / TRANSCRIPT_INDEX_FILENAME


def read_transcript_index(tree_root: Path) -> tuple[TranscriptIndexRow, ...]:
    """Return every row the tree's index holds, in append order.

    Args:
        tree_root: The tree's ``.ea`` directory.

    Returns:
        The rows; empty when nothing was indexed yet.

    Raises:
        pydantic.ValidationError: A line does not validate, so the file is corrupt.
    """
    try:
        text = transcript_index_path(tree_root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    return tuple(
        TranscriptIndexRow.model_validate_json(line) for line in text.splitlines() if line.strip()
    )


def _index_row(
    *,
    runtime: str,
    session_id: str,
    transcript_path: str,
    now: datetime,
    run_ref: str | None,
    parent_session_id: str | None,
) -> TranscriptIndexRow:
    return TranscriptIndexRow.model_validate(
        {
            "runtime": runtime,
            "vendor_session_digest": session_id,
            "transcript_path": transcript_path,
            "run_ref": run_ref,
            "parent_session_digest": parent_session_id,
            "recorded_at": now,
        }
    )


def _identity(row: TranscriptIndexRow) -> str:
    """Return what makes two rows equal: every field but when each was written."""
    return row.model_dump_json(exclude={"recorded_at"})


def _append_unless_held(
    tree_root: Path, row: TranscriptIndexRow, held: set[str]
) -> TranscriptIndexRow | None:
    """Append *row* unless *held*, the identities the index holds, names it; then hold it."""
    identity = _identity(row)
    if identity in held:
        return None
    append_json_line(transcript_index_path(tree_root), row.model_dump_json())
    held.add(identity)
    return _logged(row)


def _logged(row: TranscriptIndexRow) -> TranscriptIndexRow:
    logger.info(
        f"record_transcript runtime={row.runtime} run={row.run_ref} "
        f"subagent={row.parent_session_digest is not None}"
    )
    return row


def record_transcript(
    tree_root: Path,
    *,
    runtime: str,
    session_id: str,
    transcript_path: str,
    now: datetime,
    run_ref: str | None = None,
    parent_session_id: str | None = None,
) -> TranscriptIndexRow | None:
    """Append the row naming one session's transcript file, unless the index holds it.

    Args:
        tree_root: The tree's ``.ea`` directory.
        runtime: The harness that wrote the file, ``claude-code`` or ``codex``.
        session_id: The session's id, raw or already hashed.
        transcript_path: The file on this machine.
        now: When the row is written.
        run_ref: The Run the session served, when known.
        parent_session_id: For a subagent, the id of the session that spawned it.

    Returns:
        The appended row, or ``None`` when an equal row was already held.

    Raises:
        pydantic.ValidationError: A field fails the closed schema, such as an
            unknown runtime or an empty session id or path, or the index holds
            a corrupt line.
        StateConflict: The index's append lock could not be acquired.
    """
    row = _index_row(
        runtime=runtime,
        session_id=session_id,
        transcript_path=transcript_path,
        now=now,
        run_ref=run_ref,
        parent_session_id=parent_session_id,
    )
    identity = _identity(row)
    if not append_json_line_unless(
        transcript_index_path(tree_root),
        row.model_dump_json(),
        lambda: any(_identity(item) == identity for item in read_transcript_index(tree_root)),
    ):
        return None
    return _logged(row)


def _codex_sessions_root() -> Path:
    """Return the directory the operator's Codex keeps its rollout files in."""
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured) if configured else Path.home() / ".codex") / "sessions"


def _run_homes(tree_root: Path, dirname: str) -> list[Path]:
    """Return the per-Run home named *dirname* inside each of the tree's leased workspaces.

    A dispatched Run's runtime is given its own configuration home inside its
    workspace, so the transcript it writes is there, not in the operator's home.
    """
    # Imported here: the session hooks import this module, and only the
    # daemon's back-fill walks the Runs' workspaces.
    from eawf.runtime.workspace.lease import MCP_ARTIFACT_DIRNAME, WORKSPACE_LOCATOR

    return sorted((tree_root / WORKSPACE_LOCATOR).glob(f"*/{MCP_ARTIFACT_DIRNAME}/{dirname}"))


def _session_files(harness: HostHarness, tree_root: Path) -> Iterator[tuple[str, Path, str | None]]:
    """Yield each transcript file *harness* keeps, with its session and parent ids.

    The operator's own transcript directory is scanned first, then each
    dispatched Run's. A Claude Code subagent's file sits under its spawning
    session as ``<session>/subagents/agent-<id>.jsonl``, so the directory
    names its parent.
    """
    if harness == "claude-code":
        from eawf.runtime.runtimes.claude.managed_run import MANAGED_CONFIG_DIRNAME

        homes = _run_homes(tree_root, MANAGED_CONFIG_DIRNAME)
        for root in (projects_root(), *(home / "projects" for home in homes)):
            for path in sorted(root.glob("*/*.jsonl")):
                yield path.stem, path, None
            for path in sorted(root.glob(f"*/*/{_SUBAGENT_DIRNAME}/{_SUBAGENT_PREFIX}*.jsonl")):
                yield path.stem.removeprefix(_SUBAGENT_PREFIX), path, path.parent.parent.name
        return
    from eawf.runtime.runtimes.codex.app_server import CODEX_HOME_DIRNAME

    homes = _run_homes(tree_root, CODEX_HOME_DIRNAME)
    for root in (_codex_sessions_root(), *(home / "sessions" for home in homes)):
        for path in sorted(root.rglob("rollout-*.jsonl")):
            yield path.stem[-_CODEX_THREAD_ID_CHARS:], path, None


def backfill_transcript_index(
    tree_root: Path,
    sessions: Iterable[tuple[str, VendorSessionRef]],
    *,
    now: datetime,
) -> int:
    """Index the transcript of each Run no row names yet, found by its session digest.

    A Run started before the index existed, or whose hook never fired, names its
    vendor session only as a digest. Each file in the operator's and each
    dispatched Run's transcript directories is named by a raw session id, so
    hashing that id finds the Run's file; a file whose digest no Run carries is
    left alone. The index is read once, so the pass costs one read however many
    rows it appends.

    Args:
        tree_root: The tree's ``.ea`` directory.
        sessions: Each Run's reference and the vendor session it recorded.
        now: When the rows are written.

    Returns:
        How many rows were appended.

    Raises:
        OSError: A transcript directory could not be listed.
        pydantic.ValidationError: The index holds a line that does not validate.
        StateConflict: The index's append lock could not be acquired.
    """
    rows = read_transcript_index(tree_root)
    indexed = {row.run_ref for row in rows}
    held = {_identity(row) for row in rows}
    wanted: dict[tuple[str, str], list[str]] = {}
    for run_ref, session in sessions:
        if run_ref not in indexed:
            wanted.setdefault((session.harness, session.session_digest), []).append(run_ref)
    recorded = 0
    for harness in _HARNESSES:
        if not any(key[0] == harness for key in wanted):
            continue
        for session_id, path, parent in _session_files(harness, tree_root):
            for run_ref in wanted.get((harness, hash_vendor_session_id(session_id)), ()):
                row = _index_row(
                    runtime=harness,
                    session_id=session_id,
                    transcript_path=str(path),
                    now=now,
                    run_ref=run_ref,
                    parent_session_id=parent,
                )
                recorded += _append_unless_held(tree_root, row, held) is not None
    logger.info(f"backfill_transcript_index tree={tree_root.name} recorded={recorded}")
    return recorded


__all__ = [
    "TRANSCRIPT_INDEX_FILENAME",
    "TranscriptIndexRow",
    "backfill_transcript_index",
    "read_transcript_index",
    "record_transcript",
    "transcript_index_path",
]
