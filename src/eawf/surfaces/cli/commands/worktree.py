"""``eawf worktree`` — Typer handlers for the worktree noun group.

Subcommands:

- ``worktree create`` — branch from current HEAD, materialise a worktree
  under ``.ea/worktrees/<name>/``, append a
  :class:`~eawf.kernel.state.models.WorktreeRecord`.
- ``worktree list`` — read-only enumeration with a ``git worktree list
  --porcelain`` cross-check column.
- ``worktree merge-back`` — replay worktree commits onto the parent
  branch via cherry-pick (default) or rebase-then-fast-forward.
- ``worktree cleanup`` — tear down the worktree directory + branch,
  refusing-by-default for dirty/CONFLICTED records.
- ``worktree reconcile`` — retire active worktree rows git no longer
  lists and session rows whose holder is gone (``--dry-run`` reports).

This module also wires the wave-centric automation verbs onto the
``wave`` noun-group (imported from
:mod:`eawf.surfaces.cli.commands.lifecycle`):

- ``wave land`` — cherry-pick a wave's worktree commits onto the
  parent branch, then close the wave with the resulting SHA. This is
  the AGENTS.md-discipline-compliant entry point (cherry-pick only,
  never merge).
- ``wave land-batch`` — apply ``wave land`` to every eligible wave in
  dep order; stop on the first failure.
- ``wave autoland`` — the land back-half: cherry-pick *already-closed*
  waves' worktree commits onto the parent branch in dep order, stopping
  on the first conflict. Unlike ``wave land`` / ``wave land-batch`` it
  never drives a close (the wave is closed already); it only replays
  commits and tears the worktree down.

The mutating handlers write through the daemon: ``worktree merge-back``
and ``worktree cleanup`` route to ``state.worktree_merge_back`` /
``state.worktree_cleanup``, and the land and reconcile verbs to
``state.wave_land`` / ``state.wave_land_batch`` / ``state.wave_autoland`` /
``state.worktree_reconcile``. Each takes the git-side
:func:`eawf.runtime.worktree.locks.worktree_registry_lock` before the state
lock; the two guard disjoint paths. The in-process writer runs only under
the explicit daemonless carve-out.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.kernel.state.ids import is_wave_id
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

#: Merge-back strategy tokens — mirror
#: :data:`eawf.runtime.worktree.merge_back.STRATEGY_CHERRY_PICK` /
#: ``STRATEGY_REBASE_THEN_FF`` by value so the ``worktree merge-back
#: --strategy`` default + help text do not import the heavy ``eawf.runtime.worktree``
#: subtree at command-tree build time. The runtime ``merge_back`` call uses
#: the deferred import.
STRATEGY_CHERRY_PICK: str = "cherry_pick"
STRATEGY_REBASE_THEN_FF: str = "rebase_then_ff"


worktree_app = typer.Typer(
    name="worktree",
    help="Manage per-wave git worktrees (create / list / merge-back / cleanup / reconcile).",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_state_path(flags: GlobalFlags) -> Path:
    """Resolve the active ``state.json`` path or raise :class:`UserError` (``kind="NotFound"``)."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc


def _resolve_repo_root(state_path: Path) -> Path:
    """Resolve the repo root for ``git worktree`` calls.

    Uses the directory containing the ``.ea/`` parent of *state_path*
    as the working directory hint. ``git rev-parse --show-toplevel``
    walks up from there.
    """
    # state.json lives at <repo>/.ea/state.json; walking up two parents
    # gives us a working dir that's inside the git tree.
    from eawf.runtime.worktree.git import repo_root as resolve_repo_root

    start = state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent
    return resolve_repo_root(start)


# ---- worktree create --------------------------------------------------------


# ---- worktree list ----------------------------------------------------------


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
    from eawf.surfaces.cli import _dispatch

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
        payload = _dispatch.call_upkeep(
            "state.worktree_merge_back",
            {
                "repo_root": str(repo_root),
                "wave_id": wave,
                "strategy": strategy,
                "target": target,
                "continue_": continue_,
                "abort": abort,
            },
            flags=flags,
            verb="worktree merge-back",
            local=lambda: _merge_back_locally(
                state_path,
                repo_root,
                wave=wave,
                strategy=strategy,
                target=target,
                continue_=continue_,
                abort=abort,
            ),
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    if payload["status"] == "conflicted":
        conflict = payload["conflict"]
        text = (
            f"merge-back conflict wave={wave} strategy={payload['strategy']} "
            f"files={conflict['files']}"
        )
    else:
        text = (
            f"merge-back wave={wave} strategy={payload['strategy']} "
            f"merged={payload['merged_commit']} target={payload['target_branch']}"
        )
    emit_json_or_text(payload, text, flags=flags)


def _merge_back_locally(
    state_path: Path,
    repo_root: Path,
    *,
    wave: str,
    strategy: str,
    target: str | None,
    continue_: bool,
    abort: bool,
) -> dict[str, Any]:
    """Run one merge-back in process, under the daemonless carve-out.

    Lock order matches every worktree mutator: the registry lock first,
    then the state lock, so no two mutators ever hold the pair crossed.
    """
    from eawf.runtime.worktree import merge_back, worktree_registry_lock
    from eawf.runtime.worktree.merge_back import merge_back_payload
    from eawf.surfaces.cli._mutation import state_transaction

    with (
        worktree_registry_lock(repo_root, timeout=5.0),
        state_transaction(state_path) as state,
    ):
        return merge_back_payload(
            merge_back(
                state,
                repo_root=repo_root,
                wave_id=wave,
                strategy=strategy,
                target=target,
                continue_=continue_,
                abort=abort,
            )
        )


# ---- worktree path-fix ------------------------------------------------------


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
    from eawf.surfaces.cli import _dispatch

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
        payload = _dispatch.call_upkeep(
            "state.worktree_cleanup",
            {
                "repo_root": str(repo_root),
                "wave_id": wave,
                "force": force,
                "keep_branch": keep_branch,
            },
            flags=flags,
            verb="worktree cleanup",
            local=lambda: _cleanup_locally(
                state_path, repo_root, wave=wave, force=force, keep_branch=keep_branch
            ),
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        payload,
        f"worktree cleanup wave={wave} branch={payload['branch']} "
        f"branch_deleted={payload['branch_deleted']}",
        flags=flags,
    )


def _cleanup_locally(
    state_path: Path, repo_root: Path, *, wave: str, force: bool, keep_branch: bool
) -> dict[str, Any]:
    """Run one cleanup in process, under the daemonless carve-out (registry lock first)."""
    from eawf.runtime.worktree import cleanup_worktree, worktree_registry_lock
    from eawf.runtime.worktree.cleanup import cleanup_payload
    from eawf.surfaces.cli._mutation import state_transaction

    with (
        worktree_registry_lock(repo_root, timeout=5.0),
        state_transaction(state_path) as state,
    ):
        return cleanup_payload(
            cleanup_worktree(
                state, repo_root=repo_root, wave_id=wave, force=force, keep_branch=keep_branch
            )
        )


# ---- worktree reconcile -----------------------------------------------------


@worktree_app.command(name="reconcile")
def worktree_reconcile_cmd(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report the rows that would be retired; write nothing."),
    ] = False,
) -> None:
    """Retire active worktree and session rows whose holder is gone."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
        result = _call_worktree_daemon(
            method="state.worktree_reconcile",
            params={"repo_root": str(repo_root), "dry_run": dry_run},
            flags=flags,
            verb="worktree reconcile",
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    verb = "would retire" if dry_run else "retired"
    lines = [f"worktree reconcile {verb} {len(result['retired'])} rows"]
    lines.extend(
        f"  {verb} {row['kind']} {row['row_id']} ({row['reason']})" for row in result["retired"]
    )
    emit_json_or_text(result, "\n".join(lines), flags=flags)


def _reconcile_daemonless(*, params: dict[str, Any], flags: GlobalFlags) -> dict[str, Any]:
    """Run the reconcile locally; with no daemon, no session predates a boot."""
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.state.io import StateValidationError
    from eawf.kernel.store.paths import store_path
    from eawf.runtime.worktree import worktree_registry_lock
    from eawf.runtime.worktree.reconcile import reconcile_stale_rows

    state_path = _resolve_state_path(flags)
    repo_root = Path(str(params["repo_root"]))
    try:
        with worktree_registry_lock(repo_root, timeout=5.0):
            result = reconcile_stale_rows(
                state_path,
                store_path(state_path, StoreKind.EVENT),
                repo_root=repo_root,
                booted_at=None,
                dry_run=bool(params["dry_run"]),
            )
    except StateValidationError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc
    return {
        "retired": [
            {"kind": row.kind.value, "row_id": row.row_id, "reason": row.reason.value}
            for row in result.rows
        ],
        "dry_run": result.dry_run,
        "written": result.written,
    }


# ---- wave land --------------------------------------------------------------
# These verbs hang off ``wave_app`` (defined in lifecycle.py). We register
# them here because the implementation depends on the worktree subsystem.
# Importing ``wave_app`` rather than mutating ``lifecycle.py`` keeps the
# parallel-wave discipline intact: W01 owns lifecycle.py, this wave owns
# worktree.py.


def _call_worktree_daemon(
    *,
    method: str,
    params: dict[str, Any],
    flags: GlobalFlags,
    verb: str,
) -> dict[str, Any]:
    """Call one daemon-owned worktree mutator and map RPC errors to CLI errors."""
    from eawf.surfaces.cli import _dispatch
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    if _dispatch.daemonless_requested(flags):
        if method == "state.worktree_reconcile":
            return _reconcile_daemonless(params=params, flags=flags)
        return _call_worktree_daemonless(method=method, params=params, flags=flags)

    try:
        _dispatch.escalate_mutation(verb, flags=flags)
        with DaemonClient() as client:
            return client.call(method, params)
    except DaemonRpcError as exc:
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc


def _wave_land_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one wave-land result."""
    return {
        "wave": result.wave_id,
        "commits": list(result.commits),
        "outcome": result.outcome,
        "closed": result.closed,
        "worktree_cleaned": result.worktree_cleaned,
        "merged_commit": result.merged_commit,
        "integration_id": result.integration_id,
        "close_attempt": None,
        "close_backgrounded": False,
    }


def _wave_land_batch_payload(result: Any) -> dict[str, Any]:
    """Return explicit synchronous-compatibility daemonless batch output."""
    return {
        "landed": [_wave_land_payload(row) for row in result.landed],
        "failed_wave": result.failed_wave,
        "error": result.error,
        "skipped": list(result.skipped),
        "barrier_requirements": {
            wave_id: list(stages) for wave_id, stages in result.barrier_requirements.items()
        },
        "close_mode": "daemonless_synchronous",
    }


def _wave_autoland_row_payload(row: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one autoland row."""
    return {
        "wave": row.wave_id,
        "commits": list(row.commits),
        "merged_commit": row.merged_commit,
        "worktree_cleaned": row.worktree_cleaned,
    }


def _wave_autoland_payload(result: Any) -> dict[str, Any]:
    """Return the JSON-mode result shape for one wave-autoland result."""
    return {
        "order": list(result.order),
        "landed": [_wave_autoland_row_payload(row) for row in result.landed],
        "failed_wave": result.failed_wave,
        "error": result.error,
        "remaining": list(result.remaining),
        "dry_run": result.dry_run,
    }


def _call_worktree_daemonless(
    *,
    method: str,
    params: dict[str, Any],
    flags: GlobalFlags,
) -> dict[str, Any]:
    """Run the worktree mutator locally under the legacy daemonless carve-out."""
    from eawf.runtime.worktree import (
        wave_autoland,
        wave_land,
        wave_land_batch,
        worktree_registry_lock,
    )
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.transitions import LifecycleError

    state_path = _resolve_state_path(flags)
    repo_root = Path(str(params["repo_root"]))
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            if method == "state.wave_land":
                return _wave_land_payload(
                    wave_land(
                        state,
                        repo_root=repo_root,
                        wave_id=str(params["wave_id"]),
                        outcome=params.get("outcome"),
                        keep_worktree=bool(params.get("keep_worktree", False)),
                    )
                )
            if method == "state.wave_land_batch":
                return _wave_land_batch_payload(
                    wave_land_batch(
                        state,
                        repo_root=repo_root,
                        iter_id=params.get("iter_id"),
                        ready_only=bool(params.get("ready_only", False)),
                        keep_worktree=bool(params.get("keep_worktree", False)),
                    )
                )
            if method == "state.wave_autoland":
                return _wave_autoland_payload(
                    wave_autoland(
                        state,
                        repo_root=repo_root,
                        iter_id=params.get("iter_id"),
                        keep_worktree=bool(params.get("keep_worktree", False)),
                        dry_run=bool(params.get("dry_run", False)),
                    )
                )
    except LifecycleError as exc:
        raise cli_errors.ValidationError(str(exc)) from exc
    raise cli_errors.UserError(f"unknown worktree daemon method: {method}", kind="InvalidInput")
