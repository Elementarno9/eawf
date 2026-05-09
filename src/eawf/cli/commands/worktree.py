"""``eawf worktree`` — Typer handlers for the worktree noun group.

Subcommands:

- ``worktree create`` — branch from current HEAD, materialise a worktree
  under ``.claude/worktrees/<name>/``, append a
  :class:`~eawf.state.models.WorktreeRecord`.
- ``worktree list`` — read-only enumeration with a ``git worktree list
  --porcelain`` cross-check column.
- ``worktree merge-back`` — replay worktree commits onto the parent
  branch via cherry-pick (default) or rebase-then-fast-forward.
- ``worktree cleanup`` — tear down the worktree directory + branch,
  refusing-by-default for dirty/CONFLICTED records.

Every mutating handler runs inside
:func:`eawf.cli._mutation.state_transaction` (state-side serialisation)
*and* :func:`eawf.worktree.locks.worktree_registry_lock` (git-side
registry serialisation). The two locks compose without re-entry: the
state lock guards ``state.json`` and the registry lock guards
``.git/worktrees/<name>``; they target disjoint paths.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.state.ids import is_wave_id
from eawf.worktree import (
    STRATEGY_CHERRY_PICK,
    STRATEGY_REBASE_THEN_FF,
    cleanup_worktree,
    create_worktree,
    list_worktrees,
    merge_back,
    worktree_registry_lock,
)
from eawf.worktree.git import repo_root as resolve_repo_root

logger = logging.getLogger(__name__)


worktree_app = typer.Typer(
    name="worktree",
    help="Manage per-wave git worktrees (create / list / merge-back / cleanup).",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_state_path(flags: GlobalFlags) -> Path:
    """Resolve the active ``state.json`` path or raise :class:`NotFound`."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.NotFound(str(exc)) from exc


def _resolve_repo_root(state_path: Path) -> Path:
    """Resolve the repo root for ``git worktree`` calls.

    Uses the directory containing the ``.ea/`` parent of *state_path*
    as the working directory hint. ``git rev-parse --show-toplevel``
    walks up from there.
    """
    # state.json lives at <repo>/.ea/state.json; walking up two parents
    # gives us a working dir that's inside the git tree.
    start = state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent
    return resolve_repo_root(start)


# ---- worktree create --------------------------------------------------------


@worktree_app.command(name="create")
def worktree_create_cmd(
    ctx: typer.Context,
    wave: Annotated[
        str,
        typer.Option("--wave", help="Wave id (P\\d{2}-I\\d{2}-W\\d{2})."),
    ],
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Branch name; defaults to feature/eawf-v0.1-pNN-wMM.",
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base",
            help="Base ref to branch from; defaults to current HEAD branch.",
        ),
    ] = None,
    path: Annotated[
        Path | None,
        typer.Option(
            "--path",
            help="Target dir; defaults to .claude/worktrees/<branch-suffix>/.",
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Owner session id for provenance.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reuse an existing empty target dir.",
        ),
    ] = False,
) -> None:
    """Create a per-wave worktree branched from the current feature branch."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave!r}"),
            flags=flags,
        )
        return

    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    explicit_base = base is not None
    record = None
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            record = create_worktree(
                state,
                repo_root=repo_root,
                wave_id=wave,
                branch=branch,
                base=base,
                path=path,
                session_id=session,
                force=force,
                explicit_base=explicit_base,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    assert record is not None
    emit_json_or_text(
        {
            "worktree_id": record.id,
            "wave_id": record.wave_id,
            "branch": record.branch,
            "base_branch": record.base_branch,
            "path": record.path,
            "status": record.status.value,
            "created_at": record.created_at.isoformat(),
        },
        f"worktree create wave={record.wave_id} branch={record.branch} path={record.path}",
        flags=flags,
    )


# ---- worktree list ----------------------------------------------------------


@worktree_app.command(name="list")
def worktree_list_cmd(
    ctx: typer.Context,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Include MERGED and ABANDONED records."),
    ] = False,
) -> None:
    """Enumerate recorded worktrees with a git-side cross-check column."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    try:
        # Read-only path: no state_transaction needed; load directly.
        with state_transaction(state_path) as state:
            rows = list(list_worktrees(state, repo_root=repo_root, include_terminal=all_))
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "count": len(rows),
        "worktrees": [
            {
                "id": row.record.id,
                "wave_id": row.record.wave_id,
                "branch": row.record.branch,
                "path": row.record.path,
                "status": row.record.status.value,
                "owner_session_id": row.record.owner_session_id,
                "git_present": row.git_present,
                "created_at": row.record.created_at.isoformat(),
                "merged_commit": row.record.merged_commit,
            }
            for row in rows
        ],
    }
    if rows:
        text = "\n".join(
            f"{row.record.id} wave={row.record.wave_id} branch={row.record.branch} "
            f"path={row.record.path} status={row.record.status.value} "
            f"git_present={row.git_present}"
            for row in rows
        )
    else:
        text = "(no worktrees)"
    emit_json_or_text(payload, text, flags=flags)


# ---- worktree merge-back ----------------------------------------------------


@worktree_app.command(name="merge-back")
def worktree_merge_back_cmd(
    ctx: typer.Context,
    wave: Annotated[
        str,
        typer.Option("--wave", help="Wave id whose worktree to merge back."),
    ],
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help=f"Strategy: {STRATEGY_CHERRY_PICK} (default) | {STRATEGY_REBASE_THEN_FF}.",
        ),
    ] = STRATEGY_CHERRY_PICK,
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Target branch; defaults to record.base_branch.",
        ),
    ] = None,
    continue_: Annotated[
        bool,
        typer.Option("--continue", help="Resume after manual conflict resolution."),
    ] = False,
    abort: Annotated[
        bool,
        typer.Option("--abort", help="Abort an in-progress merge; mark ABANDONED."),
    ] = False,
) -> None:
    """Replay worktree commits onto the parent branch."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave!r}"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    result = None
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            result = merge_back(
                state,
                repo_root=repo_root,
                wave_id=wave,
                strategy=strategy,
                target=target,
                continue_=continue_,
                abort=abort,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    assert result is not None
    payload: dict[str, Any]
    if result.conflicted:
        payload = {
            "worktree_id": result.record.id,
            "strategy": result.strategy,
            "conflict": {
                "stage": result.strategy,
                "commit": result.conflict_commit,
                "files": result.conflict_files,
                "next_step": (
                    "resolve in parent worktree, then "
                    "`eawf worktree merge-back --wave ... --continue`"
                ),
            },
            "status": "conflicted",
        }
        text = (
            f"merge-back conflict wave={wave} strategy={result.strategy} "
            f"files={result.conflict_files}"
        )
    else:
        payload = {
            "worktree_id": result.record.id,
            "strategy": result.strategy,
            "picked_commits": result.picked_commits,
            "target_branch": result.target_branch,
            "merged_commit": result.merged_commit,
            "status": result.record.status.value,
        }
        text = (
            f"merge-back wave={wave} strategy={result.strategy} "
            f"merged={result.merged_commit} target={result.target_branch}"
        )
    emit_json_or_text(payload, text, flags=flags)


# ---- worktree cleanup -------------------------------------------------------


@worktree_app.command(name="cleanup")
def worktree_cleanup_cmd(
    ctx: typer.Context,
    wave: Annotated[
        str,
        typer.Option("--wave", help="Wave id whose worktree to remove."),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Remove even when dirty / CONFLICTED."),
    ] = False,
    keep_branch: Annotated[
        bool,
        typer.Option("--keep-branch", help="Do not delete the per-wave branch."),
    ] = False,
) -> None:
    """Tear down the worktree directory + per-wave branch."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave!r}"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    result = None
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            result = cleanup_worktree(
                state,
                repo_root=repo_root,
                wave_id=wave,
                force=force,
                keep_branch=keep_branch,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    assert result is not None
    emit_json_or_text(
        {
            "worktree_id": result.record.id,
            "removed_path": result.removed_path,
            "branch_deleted": result.branch_deleted,
            "branch": result.branch,
            "status": result.record.status.value,
        },
        f"worktree cleanup wave={wave} branch={result.branch} "
        f"branch_deleted={result.branch_deleted}",
        flags=flags,
    )


__all__ = [
    "worktree_app",
]
