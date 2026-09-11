"""Proving nobody else is holding the tree before the cutover takes it.

The apply reads every authority surface once and writes a generation
from what it read. Anything still live between that read and the select
can mutate a surface the generation was derived from, and the result is
a tree whose manifest describes a corpus that no longer exists.

Four holders are checked, and the refusal names every one it found
rather than the first. An operator clearing a cutover has to clear all
of them; a probe that stopped at the first would make that a loop of
re-runs. The check is deliberately conservative about lock records: any
lease file present refuses, without asking whether its holder still
breathes. Reaping a stale lock is a decision about somebody else's work,
and a one-shot migration is the wrong place to make it.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.errors import MigrationNotQuiescentError
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.runtime.daemon.wal import WalStatus, list_records

logger = logging.getLogger(__name__)


#: The document collection holding agent sessions.
SESSIONS_COLLECTION: Final = "agent_sessions"

#: The document collection holding managed worktrees.
WORKTREES_COLLECTION: Final = "worktrees"

#: The row field both collections record their liveness in.
STATUS_FIELD: Final = "status"

#: The status value that means a session or worktree is still live.
ACTIVE_STATUS: Final = "active"

#: Where lease records live inside the tree.
LOCKS_DIRNAME: Final = "locks"

#: The lease filename suffix.
LOCK_SUFFIX: Final = ".lock"

#: Where the write-ahead log lives inside the tree, relative to the root.
WAL_LOCATOR: Final = "locks/wal"

#: The write-ahead statuses that block a cutover. A ``fsynced`` record is
#: already durable and is merely waiting for the collector, so it holds
#: nothing; a ``pending`` or ``applied`` record is a mutation mid-flight.
BLOCKING_WAL_STATUSES: Final[tuple[WalStatus, ...]] = (WalStatus.PENDING, WalStatus.APPLIED)

#: How many holders the refusal names before it truncates.
MAX_NAMED_HOLDERS: Final = 20


class QuiescenceHolderKind(StrEnum):
    """The four things that can still be holding a tree."""

    ACTIVE_SESSION = "active_session"
    HELD_LEASE = "held_lease"
    PENDING_WAL_RECORD = "pending_wal_record"
    MANAGED_WORKTREE = "managed_worktree"


class QuiescenceFinding(StrictMigrationModel):
    """One holder the probe found, addressed so an operator can clear it.

    Attributes:
        kind: Which holder it is.
        locator: Where it was found, as a tree-relative address. Never an
            absolute path -- a refusal an operator pastes into a ticket
            should not carry their home directory.
        detail: What the holder says about itself.
    """

    kind: QuiescenceHolderKind
    locator: Annotated[str, Field(min_length=1, max_length=200)]
    detail: Annotated[str, Field(min_length=1, max_length=200)]


def _document_rows(document: Any, collection: str) -> dict[str, Any]:
    """Return one collection's rows from a decoded document.

    Args:
        document: The decoded document, which may be any JSON value.
        collection: The collection key to read.

    Returns:
        The rows, or an empty mapping when the document holds no such
        collection or holds something other than an object under it. An
        unreadable shape is not a holder: the residency gates own that
        complaint, and reporting it twice would make one defect look
        like two.
    """
    if not isinstance(document, dict):
        return {}
    rows = document.get(collection)
    return rows if isinstance(rows, dict) else {}


def _active_rows(
    document: Any, *, collection: str, kind: QuiescenceHolderKind
) -> list[QuiescenceFinding]:
    """Return a finding per row of ``collection`` that is still active."""
    findings: list[QuiescenceFinding] = []
    rows = _document_rows(document, collection)
    for key in sorted(rows):
        row = rows[key]
        status = row.get(STATUS_FIELD) if isinstance(row, dict) else None
        if status == ACTIVE_STATUS:
            findings.append(
                QuiescenceFinding(
                    kind=kind,
                    locator=f"state.json:{collection}/{key}",
                    detail=f"{STATUS_FIELD}={ACTIVE_STATUS}",
                )
            )
    return findings


def _read_document(root: Path) -> Any:
    """Return the decoded target document, or ``None`` when unreadable.

    Args:
        root: The target tree's root.

    Returns:
        The decoded document. A missing or malformed document decodes to
        ``None``: a tree with no document holds no session and no
        worktree, which is exactly what the probe needs to answer.
    """
    path = root / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        logger.debug(f"_read_document unreadable root={root.name}")
        return None


def _lease_findings(root: Path) -> list[QuiescenceFinding]:
    """Return a finding per lease record under the tree's lock directory."""
    locks = root / LOCKS_DIRNAME
    if not locks.is_dir():
        return []
    return [
        QuiescenceFinding(
            kind=QuiescenceHolderKind.HELD_LEASE,
            locator=f"{LOCKS_DIRNAME}/{path.name}",
            detail="a lease record is present; clear it before the cutover",
        )
        for path in sorted(locks.iterdir())
        if path.is_file() and path.name.endswith(LOCK_SUFFIX)
    ]


def _wal_findings(root: Path) -> list[QuiescenceFinding]:
    """Return a finding per write-ahead record that is still in flight."""
    wal_dir = root / WAL_LOCATOR
    findings: list[QuiescenceFinding] = []
    for status in BLOCKING_WAL_STATUSES:
        findings.extend(
            QuiescenceFinding(
                kind=QuiescenceHolderKind.PENDING_WAL_RECORD,
                locator=f"{WAL_LOCATOR}/{path.name}",
                detail=f"write-ahead record in status {status.value}",
            )
            for path in list_records(wal_dir, status)
        )
    return findings


def quiescence_findings(target: DisposableTarget) -> tuple[QuiescenceFinding, ...]:
    """Return every holder still keeping the target tree busy.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The findings, grouped by holder kind in declaration order and
        addressed by tree-relative locator.
    """
    document = _read_document(target.root)
    findings = [
        *_active_rows(
            document,
            collection=SESSIONS_COLLECTION,
            kind=QuiescenceHolderKind.ACTIVE_SESSION,
        ),
        *_lease_findings(target.root),
        *_wal_findings(target.root),
        *_active_rows(
            document,
            collection=WORKTREES_COLLECTION,
            kind=QuiescenceHolderKind.MANAGED_WORKTREE,
        ),
    ]
    logger.info(f"quiescence_findings root={target.root.name} holders={len(findings)}")
    return tuple(findings)


def require_quiescent(findings: tuple[QuiescenceFinding, ...]) -> None:
    """Refuse a cutover while anything is still holding the tree.

    Args:
        findings: What the probe reported.

    Raises:
        MigrationNotQuiescentError: At least one holder is live. The
            message itemises each one by kind and locator, up to
            :data:`MAX_NAMED_HOLDERS`, and always reports the true total.
    """
    if not findings:
        return
    named = ", ".join(
        f"{finding.kind.value} {finding.locator} ({finding.detail})"
        for finding in findings[:MAX_NAMED_HOLDERS]
    )
    suffix = "" if len(findings) <= MAX_NAMED_HOLDERS else ", ..."
    raise MigrationNotQuiescentError(
        f"{len(findings)} holders are still live, so the cutover would race them: {named}{suffix}"
    )


__all__ = [
    "BLOCKING_WAL_STATUSES",
    "MAX_NAMED_HOLDERS",
    "QuiescenceFinding",
    "QuiescenceHolderKind",
    "quiescence_findings",
    "require_quiescent",
]
