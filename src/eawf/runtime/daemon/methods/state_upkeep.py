"""Daemon-owned epoch-1 upkeep: session close and recover, worktree merge-back and cleanup.

A tree still in epoch 1 has to settle its live sessions and worktrees
before the cutover, so these four verbs outlive the flag day. Each runs its
mutator through :func:`commit_worktree_state`, so the write takes the same
lock, WAL record and event row as every other epoch-1 mutation the daemon
makes, and the path resolver refuses a tree that carries the epoch marker.

A mutator refusal is answered as a typed ``refusal`` rather than a
JSON-RPC error: the operator-facing error class and kind (a dirty worktree
is an integrity conflict, a missing session is not found) have no wire
code of their own, and flattening them would change the exit code the
verb has always had.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import AgentSessionStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.state_context import resolve_mutator_paths
from eawf.runtime.daemon.methods.state_worktree import commit_worktree_state
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)

SESSION_CLOSE_METHOD: Final = "state.session_close"
SESSION_RECOVER_METHOD: Final = "state.session_recover"
WORKTREE_MERGE_BACK_METHOD: Final = "state.worktree_merge_back"
WORKTREE_CLEANUP_METHOD: Final = "state.worktree_cleanup"

#: The refusal classes an answer may name, and the one each rebuilds as.
REFUSAL_CLASSES: Final[dict[str, type[cli_errors.CliError]]] = {
    "UserError": cli_errors.UserError,
    "StateConflict": cli_errors.StateConflict,
    "ValidationError": cli_errors.ValidationError,
}

_Text = Field(min_length=1)


class SessionCloseParams(BaseModel):
    """Params of :data:`SESSION_CLOSE_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = _Text
    session_id: str = _Text
    status: Literal["closed", "stale", "failed"]
    summary: str | None = None


class SessionRecoverParams(BaseModel):
    """Params of :data:`SESSION_RECOVER_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = _Text
    age_minutes: int = Field(ge=0)


class WorktreeMergeBackParams(BaseModel):
    """Params of :data:`WORKTREE_MERGE_BACK_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = _Text
    wave_id: str = _Text
    strategy: str = _Text
    target: str | None = None
    continue_: bool = False
    abort: bool = False


class WorktreeCleanupParams(BaseModel):
    """Params of :data:`WORKTREE_CLEANUP_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = _Text
    wave_id: str = _Text
    force: bool = False
    keep_branch: bool = False


class _Refused(Exception):  # noqa: N818 — carries a refusal past the commit wrapper
    """A mutator refusal, kept whole so the wrapper does not flatten it."""

    def __init__(self, error: cli_errors.CliError) -> None:
        super().__init__(str(error))
        self.error = error


def refusal_answer(error: cli_errors.CliError) -> dict[str, Any]:
    """Return the answer a refused upkeep verb carries in place of its result."""
    return {
        "refusal": {
            "error_class": type(error).__name__,
            "kind": error.kind,
            "message": str(error),
        }
    }


def _guarded(apply: Callable[[State], dict[str, Any]]) -> Callable[[State], dict[str, Any]]:
    """Wrap *apply* so a CLI-typed refusal reaches the caller unflattened."""

    def run(state: State) -> dict[str, Any]:
        try:
            return apply(state)
        except cli_errors.CliError as error:
            if type(error).__name__ not in REFUSAL_CLASSES:
                raise
            raise _Refused(error) from error

    return run


def _commit(
    ctx: MethodContext,
    *,
    params: dict[str, Any],
    repo_root: str,
    command: str,
    scope_id: str | None,
    apply: Callable[[State], dict[str, Any]],
) -> dict[str, Any]:
    """Commit one upkeep mutator, or answer its refusal with nothing written."""
    try:
        return commit_worktree_state(
            ctx=ctx,
            repo_root=Path(repo_root),
            params=params,
            command=command,
            scope_id=scope_id,
            apply_func=_guarded(apply),
        )
    except _Refused as refused:
        logger.info(f"upkeep refused command={command} scope_id={scope_id!r}")
        return refusal_answer(refused.error)


def _session_close(state: State, args: SessionCloseParams, events_path: Path) -> dict[str, Any]:
    from eawf.runtime.session.store import SessionNotFound, close_session

    try:
        result = close_session(
            state=state,
            events_path=events_path,
            session_id=args.session_id,
            status=AgentSessionStatus(args.status),
            summary=args.summary,
        )
    except SessionNotFound as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc
    ended = result.session.ended_at
    return {
        "id": result.session.id,
        "status": result.session.status.value,
        "ended_at": ended.isoformat() if ended is not None else None,
    }


@register(SESSION_CLOSE_METHOD)
async def session_close_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Move one epoch-1 session to a terminal status."""
    args = SessionCloseParams.model_validate(params)
    _state_path, events_path, _wal = resolve_mutator_paths(repo_root=args.repo_root, ctx=ctx)
    return _commit(
        ctx,
        params=params,
        repo_root=args.repo_root,
        command=SESSION_CLOSE_METHOD,
        scope_id=args.session_id,
        apply=lambda state: _session_close(state, args, events_path),
    )


def _session_recover(state: State, args: SessionRecoverParams, events_path: Path) -> dict[str, Any]:
    from eawf.runtime.session.recovery import (
        record_recovery_summary,
        recover_sessions,
        recovery_payload,
    )

    report = recover_sessions(state=state, events_path=events_path, age_minutes=args.age_minutes)
    record_recovery_summary(events_path=events_path, report=report, now=datetime.now(UTC))
    return recovery_payload(report)


@register(SESSION_RECOVER_METHOD)
async def session_recover_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Mark every epoch-1 session whose heartbeat is older than the threshold stale."""
    args = SessionRecoverParams.model_validate(params)
    _state_path, events_path, _wal = resolve_mutator_paths(repo_root=args.repo_root, ctx=ctx)
    return _commit(
        ctx,
        params=params,
        repo_root=args.repo_root,
        command=SESSION_RECOVER_METHOD,
        scope_id=None,
        apply=lambda state: _session_recover(state, args, events_path),
    )


@register(WORKTREE_MERGE_BACK_METHOD)
async def worktree_merge_back_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Replay one epoch-1 wave worktree's commits onto its parent branch."""
    from eawf.runtime.worktree import merge_back, worktree_registry_lock
    from eawf.runtime.worktree.merge_back import merge_back_payload

    args = WorktreeMergeBackParams.model_validate(params)
    repo_root = Path(args.repo_root)
    with worktree_registry_lock(repo_root, timeout=5.0):
        return _commit(
            ctx,
            params=params,
            repo_root=args.repo_root,
            command=WORKTREE_MERGE_BACK_METHOD,
            scope_id=args.wave_id,
            apply=lambda state: merge_back_payload(
                merge_back(
                    state,
                    repo_root=repo_root,
                    wave_id=args.wave_id,
                    strategy=args.strategy,
                    target=args.target,
                    continue_=args.continue_,
                    abort=args.abort,
                )
            ),
        )


@register(WORKTREE_CLEANUP_METHOD)
async def worktree_cleanup_rpc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Tear down one epoch-1 wave worktree and its branch."""
    from eawf.runtime.worktree import cleanup_worktree, worktree_registry_lock
    from eawf.runtime.worktree.cleanup import cleanup_payload

    args = WorktreeCleanupParams.model_validate(params)
    repo_root = Path(args.repo_root)
    with worktree_registry_lock(repo_root, timeout=5.0):
        return _commit(
            ctx,
            params=params,
            repo_root=args.repo_root,
            command=WORKTREE_CLEANUP_METHOD,
            scope_id=args.wave_id,
            apply=lambda state: cleanup_payload(
                cleanup_worktree(
                    state,
                    repo_root=repo_root,
                    wave_id=args.wave_id,
                    force=args.force,
                    keep_branch=args.keep_branch,
                )
            ),
        )


__all__ = [
    "REFUSAL_CLASSES",
    "SESSION_CLOSE_METHOD",
    "SESSION_RECOVER_METHOD",
    "WORKTREE_CLEANUP_METHOD",
    "WORKTREE_MERGE_BACK_METHOD",
    "SessionCloseParams",
    "SessionRecoverParams",
    "WorktreeCleanupParams",
    "WorktreeMergeBackParams",
    "refusal_answer",
    "session_close_rpc",
    "session_recover_rpc",
    "worktree_cleanup_rpc",
    "worktree_merge_back_rpc",
]
