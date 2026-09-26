"""Retiring document rows whose holder has already gone away.

A worktree row stays ``active`` when its directory was removed outside
the land path (a manual ``git worktree remove``, a wiped checkout, a row
cherry-picked in from another machine), and a session row stays
``active`` when the process that owned it died without closing it. Both
read as live holders to the cutover's quiescence probe, which refuses
until they are cleared.

The reconcile only retires what git and the daemon can vouch is gone. A
worktree row is stale when ``git worktree list`` does not report its
path; a worktree git still lists is kept, whatever the document says,
because somebody may still be writing in it. A session row is stale when
every worktree it was bound to is gone, or when it predates the running
daemon's boot -- the same rule the boot sweep applies, since a fresh
daemon owns no children that started before it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import eawf.runtime.worktree.git as git
from eawf.kernel.state.enums import AgentSessionStatus, WorktreeStatus
from eawf.kernel.state.io import append_event, state_version, write_state_unlocked
from eawf.kernel.state.models import State
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)

#: The command name the reconcile records on its event row.
RECONCILE_COMMAND = "state.worktree_reconcile"

#: Worktree statuses that can still be holding a checkout.
_LIVE_WORKTREE_STATUSES = frozenset({WorktreeStatus.ACTIVE, WorktreeStatus.CONFLICTED})


class StaleRowKind(StrEnum):
    """Which document collection a retired row came from."""

    WORKTREE = "worktree"
    SESSION = "session"


class StaleReason(StrEnum):
    """Why a row was judged to hold nothing any more."""

    NO_GIT_WORKTREE = "no_git_worktree"
    BOUND_WORKTREES_GONE = "bound_worktrees_gone"
    PREDATES_DAEMON_BOOT = "predates_daemon_boot"


@dataclass(frozen=True)
class StaleRow:
    """One row the reconcile retires, with the reason it was judged stale.

    Attributes:
        kind: The collection the row lives in.
        row_id: The row's key in that collection.
        reason: Why the row holds nothing.
    """

    kind: StaleRowKind
    row_id: str
    reason: StaleReason


@dataclass(frozen=True)
class ReconcileResult:
    """What one reconcile run retired, or would retire under a dry run.

    Attributes:
        rows: Every stale row, worktrees first, each sorted by key.
        dry_run: ``True`` when nothing was written.
        written: ``True`` when the document was rewritten.
        updated_at: The ``updated_at`` stamped on the written document,
            or ``None`` when nothing was written.
    """

    rows: tuple[StaleRow, ...]
    dry_run: bool
    written: bool
    updated_at: datetime | None


def git_worktree_paths(repo_root: Path) -> frozenset[Path]:
    """Return every checkout path ``git worktree list`` reports, resolved.

    Args:
        repo_root: Any directory inside the repository.

    Returns:
        The resolved paths, the main checkout included.

    Raises:
        eawf.surfaces.cli.errors.StateConflict: When git cannot list the
            worktrees. The failure propagates rather than reading as "no
            worktrees", which would retire every row in the document.
    """
    return frozenset(
        Path(entry["worktree"]).resolve()
        for entry in git.worktree_list(repo_root)
        if entry.get("worktree")
    )


def plan_stale_rows(
    state: State,
    *,
    repo_root: Path,
    git_paths: frozenset[Path],
    booted_at: datetime | None,
) -> tuple[StaleRow, ...]:
    """Return every active row whose holder is gone, without mutating anything.

    Args:
        state: The loaded document.
        repo_root: The repository root relative worktree paths hang off.
        git_paths: The resolved checkout paths git reports.
        booted_at: When the running daemon booted, or ``None`` when no
            daemon is running (the boot rule then retires nothing).

    Returns:
        The stale rows, worktrees first, each group sorted by key.
    """
    root = repo_root.resolve()
    worktrees = state.worktrees or {}
    stale: list[StaleRow] = []
    live_worktree_ids: set[str] = set()
    for key in sorted(worktrees):
        record = worktrees[key]
        candidate = Path(record.path)
        present = (
            candidate if candidate.is_absolute() else root / candidate
        ).resolve() in git_paths
        if present and record.status in _LIVE_WORKTREE_STATUSES:
            live_worktree_ids.add(key)
        if record.status is WorktreeStatus.ACTIVE and not present:
            stale.append(StaleRow(StaleRowKind.WORKTREE, key, StaleReason.NO_GIT_WORKTREE))
    for key in sorted(state.agent_sessions):
        session = state.agent_sessions[key]
        if session.status is not AgentSessionStatus.ACTIVE:
            continue
        if session.worktree_ids and not live_worktree_ids.intersection(session.worktree_ids):
            stale.append(StaleRow(StaleRowKind.SESSION, key, StaleReason.BOUND_WORKTREES_GONE))
        elif booted_at is not None and session.started_at < booted_at:
            stale.append(StaleRow(StaleRowKind.SESSION, key, StaleReason.PREDATES_DAEMON_BOOT))
    return tuple(stale)


def _retire(state: State, rows: tuple[StaleRow, ...], *, events_path: Path) -> None:
    """Move each stale row to its terminal status in place."""
    from eawf.runtime.session.store import close_session

    for row in rows:
        if row.kind is StaleRowKind.WORKTREE:
            assert state.worktrees is not None
            state.worktrees[row.row_id].status = WorktreeStatus.ABANDONED
        else:
            close_session(
                state=state,
                events_path=events_path,
                session_id=row.row_id,
                status=AgentSessionStatus.STALE,
                summary=f"reconciled: {row.reason.value}",
            )


def reconcile_stale_rows(
    state_path: Path,
    events_path: Path,
    *,
    repo_root: Path,
    booted_at: datetime | None,
    dry_run: bool,
    check_loaded: Callable[[State], None] | None = None,
) -> ReconcileResult:
    """Retire every active worktree and session row whose holder is gone.

    Runs the whole read-modify-write under the ``state.json`` lock and
    persists through :func:`write_state_unlocked`, so a leaking payload is
    refused there and nothing is written. A run that finds nothing stale
    writes nothing, which makes a second run a no-op.

    Args:
        state_path: The ``state.json`` to reconcile.
        events_path: The event ledger session closes and the summary
            event are appended to.
        repo_root: The repository whose ``git worktree list`` is the
            authority on which checkouts exist.
        booted_at: When the running daemon booted, or ``None`` outside a
            daemon.
        dry_run: Report what would be retired without writing anything.
        check_loaded: Called with the loaded document under the lock,
            before anything is planned; raising refuses the run. The
            daemon passes its regressed-state refusal here.

    Returns:
        The itemised result.

    Raises:
        FileNotFoundError: When *state_path* does not exist.
        eawf.surfaces.cli.errors.StateConflict: When git cannot list the
            worktrees.
        eawf.kernel.state.io.StateValidationError: When the rewritten
            document would carry a leak shape.
    """
    from eawf.workflow.evidence._io import load_state

    if not state_path.exists():
        raise FileNotFoundError(f"state file not found: {state_path.name}")
    git_paths = git_worktree_paths(repo_root)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        if check_loaded is not None:
            check_loaded(state)
        rows = plan_stale_rows(state, repo_root=repo_root, git_paths=git_paths, booted_at=booted_at)
        if dry_run or not rows:
            logger.info(f"reconcile_stale_rows stale={len(rows)} dry_run={dry_run} written=False")
            return ReconcileResult(rows=rows, dry_run=dry_run, written=False, updated_at=None)
        before_version = state_version(state.model_dump(mode="json"))
        _retire(state, rows, events_path=events_path)
        state.updated_at = datetime.now(UTC)
        payload = state.model_dump(mode="json")
        write_state_unlocked(state_path, payload)
        append_event(
            events_path,
            command=RECONCILE_COMMAND,
            args={"retired": [f"{row.kind.value}:{row.row_id}" for row in rows]},
            scope_id=state.project.code if state.project is not None else "project",
            before_version=before_version,
            after_version=state_version(payload),
            summary=f"reconciled {len(rows)} stale worktree and session rows",
        )
    logger.info(f"reconcile_stale_rows stale={len(rows)} dry_run=False written=True")
    return ReconcileResult(rows=rows, dry_run=False, written=True, updated_at=state.updated_at)


__all__ = [
    "RECONCILE_COMMAND",
    "ReconcileResult",
    "StaleReason",
    "StaleRow",
    "StaleRowKind",
    "git_worktree_paths",
    "plan_stale_rows",
    "reconcile_stale_rows",
]
