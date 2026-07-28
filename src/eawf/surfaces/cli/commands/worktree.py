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

Most mutating handlers run inside
:func:`eawf.surfaces.cli._mutation.state_transaction` (state-side serialisation)
*and* :func:`eawf.runtime.worktree.locks.worktree_registry_lock` (git-side
registry serialisation). The two locks compose without re-entry: the
state lock guards ``state.json`` and the registry lock guards
``.git/worktrees/<name>``; they target disjoint paths.
``wave land``, ``wave land-batch``, and ``wave autoland`` are
daemon-owned exceptions: their state writes route through
``state.wave_land`` / ``state.wave_land_batch`` / ``state.wave_autoland``
so the daemon remains the canonical state mutator.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any

import typer

from eawf.kernel.state.ids import is_iter_id, is_wave_id
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import wave_app
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
    help="Manage per-wave git worktrees (create / list / merge-back / cleanup).",
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


@worktree_app.command(name="create")
def worktree_create_cmd(
    ctx: typer.Context,
    wave: Annotated[
        str,
        typer.Option("--wave", help="Wave id (P\\d{2,}-I\\d{2,}-W\\d{2,})."),
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
            help="Target dir; defaults to .ea/worktrees/<branch-suffix>/.",
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
    from eawf.runtime.worktree import create_worktree, worktree_registry_lock
    from eawf.surfaces.cli._mutation import state_transaction

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
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    explicit_base = base is not None
    record = None
    # Lock ordering invariant: every worktree mutator acquires the
    # registry lock FIRST, then the state-transaction lock. The two
    # target disjoint paths so they never deadlock on themselves, but
    # mixing the order across handlers would deadlock against a sibling
    # mutator holding the opposite pair. Always: registry → state.
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
    from eawf.runtime.worktree import list_worktrees
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    try:
        # Read-only listing: state_transaction held for a consistent
        # snapshot against concurrent cleanup/create mutations.
        # read_only=True bypasses the §5.5 --daemonless mutating-verb gate
        # so this read still honours the daemon-bypass carve-out.
        with state_transaction(state_path, read_only=True) as state:
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
    from eawf.runtime.worktree import merge_back, worktree_registry_lock
    from eawf.surfaces.cli._mutation import state_transaction

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
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    result = None
    # Lock ordering invariant: every worktree mutator acquires the
    # registry lock FIRST, then the state-transaction lock. The two
    # target disjoint paths so they never deadlock on themselves, but
    # mixing the order across handlers would deadlock against a sibling
    # mutator holding the opposite pair. Always: registry → state.
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


# ---- worktree path-fix ------------------------------------------------------


def _is_path_absolute_any_platform(path_str: str) -> bool:
    """True iff *path_str* is absolute on POSIX **or** Windows.

    ``pathlib.Path`` is platform-bound: a POSIX-rooted string like
    ``/foo/bar`` parses as a relative ``WindowsPath`` on Windows, and
    a drive-letter string like ``C:\\foo`` parses as a relative
    ``PosixPath`` on macOS / Linux. Worktree state.json files are
    portable, so the path-fix sweep must recognise either dialect.
    Same pattern as :func:`eawf.workflow.evidence.artifact._validate_artifact_location`.
    """
    return PurePosixPath(path_str).is_absolute() or PureWindowsPath(path_str).is_absolute()


@worktree_app.command(name="path-fix")
def worktree_path_fix_cmd(
    ctx: typer.Context,
    apply_all: Annotated[
        bool,
        typer.Option("--all", help="Rewrite every record whose path is absolute."),
    ] = False,
) -> None:
    """Rewrite WorktreeRecord.path values from absolute to repo-relative.

    Legacy fix-up: ``worktree create`` historically stored absolute paths,
    which leaks machine-local prefixes (e.g. ``/Users/...``) into the
    committed ``state.json``. The writer now stores repo-relative paths
    by default. This verb walks ``state.worktrees`` and rewrites any
    remaining absolute path that resolves inside ``repo_root``.
    """
    from eawf.runtime.worktree import worktree_registry_lock
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    if not apply_all:
        cli_errors.emit_error(
            cli_errors.UserError("pass --all to rewrite every absolute path", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path).resolve()
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    rewritten: list[dict[str, str]] = []
    with (
        worktree_registry_lock(repo_root, timeout=5.0),
        state_transaction(state_path) as state,
    ):
        if state.worktrees is None:
            state.worktrees = {}
        for wt_id, record in state.worktrees.items():
            if not _is_path_absolute_any_platform(record.path):
                continue
            current = Path(record.path)
            try:
                rel = current.resolve().relative_to(repo_root)
            except ValueError:
                continue
            rewritten.append({"id": wt_id, "from": record.path, "to": str(rel)})
            record.path = str(rel)

    emit_json_or_text(
        {"rewritten": rewritten, "count": len(rewritten)},
        f"worktree path-fix: rewrote {len(rewritten)} record(s)",
        flags=flags,
    )


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
    from eawf.runtime.worktree import cleanup_worktree, worktree_registry_lock
    from eawf.surfaces.cli._mutation import state_transaction

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
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    result = None
    # Lock ordering invariant: every worktree mutator acquires the
    # registry lock FIRST, then the state-transaction lock. The two
    # target disjoint paths so they never deadlock on themselves, but
    # mixing the order across handlers would deadlock against a sibling
    # mutator holding the opposite pair. Always: registry → state.
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


@wave_app.command(name="land")
def wave_land_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to land.")],
    outcome: Annotated[
        str | None,
        typer.Option(
            "--outcome",
            help=(
                "Outcome text stamped on the wave; defaults to a synthesised "
                "summary based on the picked-commit count."
            ),
        ),
    ] = None,
    keep_worktree: Annotated[
        bool,
        typer.Option(
            "--keep-worktree",
            help="Skip the post-close worktree cleanup.",
        ),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for the submitted close attempt."),
    ] = False,
    detach: Annotated[
        bool,
        typer.Option("--detach", help="Return after close submission."),
    ] = False,
) -> None:
    """Cherry-pick the wave's worktree commits onto the parent branch.

    Always uses cherry-pick (per AGENTS.md rule 11). On conflict, the
    wave is *not* closed and the on-disk repo state is preserved so the
    operator can resolve and re-run.
    """
    flags: GlobalFlags = ctx.obj
    if wait and detach:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--wait and --detach are mutually exclusive",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    try:
        result = _call_worktree_daemon(
            method="state.wave_land",
            params={
                "repo_root": str(repo_root),
                "wave_id": wave_id,
                "outcome": outcome,
                "keep_worktree": keep_worktree,
            },
            flags=flags,
            verb="wave land",
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    close_attempt = result.get("close_attempt")
    if close_attempt is not None:
        wait_for_terminal = wait or (
            not detach and not (sys.stdin.isatty() and sys.stdout.isatty())
        )
        if wait_for_terminal:
            from eawf.surfaces.cli.commands.close import wait_for_close

            try:
                close_result = wait_for_close(
                    ref=str(close_attempt["id"]),
                    flags=flags,
                )
            except cli_errors.CliError as err:
                cli_errors.emit_error(err, flags=flags)
                return
            result["close_attempt"] = close_result["attempt"]
            if close_result["attempt"]["status"] != "closed":
                emit_json_or_text(
                    result,
                    (
                        f"wave land {wave_id} integrated but close "
                        f"{close_result['attempt']['status']}"
                    ),
                    flags=flags,
                )
                raise typer.Exit(code=3)

    payload: dict[str, Any] = {
        "wave": result["wave"],
        "commits": list(result["commits"]),
        "outcome": result["outcome"],
        "worktree_cleaned": result["worktree_cleaned"],
        "merged_commit": result["merged_commit"],
        "integration_id": result.get("integration_id"),
        "close_attempt": result.get("close_attempt"),
        "close_backgrounded": result.get("close_backgrounded", False),
    }
    text = (
        f"wave land {wave_id} commits={payload['commits']} "
        f"outcome={payload['outcome']!r} cleaned={payload['worktree_cleaned']}"
    )
    emit_json_or_text(payload, text, flags=flags)


@wave_app.command(name="land-batch")
def wave_land_batch_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option(
            "--iter",
            help="Scope the batch to waves in this iter id.",
        ),
    ] = None,
    ready_only: Annotated[
        bool,
        typer.Option(
            "--ready-only",
            help="Skip waves whose configured land dependency barriers are not satisfied.",
        ),
    ] = False,
    keep_worktree: Annotated[
        bool,
        typer.Option(
            "--keep-worktree",
            help="Skip the post-close worktree cleanup for each landed wave.",
        ),
    ] = False,
) -> None:
    """Apply ``wave land`` to every eligible wave in dep order; stop on failure."""
    flags: GlobalFlags = ctx.obj
    if iter_flag is not None and not is_iter_id(iter_flag):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_flag!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    try:
        batch_result = _call_worktree_daemon(
            method="state.wave_land_batch",
            params={
                "repo_root": str(repo_root),
                "iter_id": iter_flag,
                "ready_only": ready_only,
                "keep_worktree": keep_worktree,
            },
            flags=flags,
            verb="wave land-batch",
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    landed_payload = [
        {
            "wave": r["wave"],
            "commits": list(r["commits"]),
            "outcome": r["outcome"],
            "closed": r["closed"],
            "worktree_cleaned": r["worktree_cleaned"],
            "merged_commit": r["merged_commit"],
            "integration_id": r.get("integration_id"),
            "close_attempt": r.get("close_attempt"),
            "close_backgrounded": r.get("close_backgrounded", False),
        }
        for r in batch_result["landed"]
    ]
    payload: dict[str, Any] = {
        "landed": landed_payload,
        "failed_wave": batch_result["failed_wave"],
        "error": batch_result["error"],
        "skipped": list(batch_result["skipped"]),
        "barrier_requirements": dict(batch_result.get("barrier_requirements", {})),
        "close_mode": batch_result.get("close_mode", "durable_async"),
    }
    landed_count = len(batch_result["landed"])
    attempts = [
        row["close_attempt"]["id"] for row in landed_payload if row["close_attempt"] is not None
    ]
    if batch_result["failed_wave"] is None:
        text = (
            f"wave land-batch integrated={landed_count} "
            f"close_mode={payload['close_mode']} attempts={attempts} "
            f"skipped={batch_result['skipped']}"
        )
        emit_json_or_text(payload, text, flags=flags)
        return

    # Partial-batch failure path. The successful prefix is persisted
    # (state_transaction committed the prior mutations); the envelope
    # carries both the landed prefix and the failing wave so the
    # operator can resolve and re-run on the remainder. Exit code 4
    # (VALIDATION_FAILED) is the conservative choice — batch invariant
    # was not satisfied — and matches how individual ``wave land``
    # surfaces close-time failures.
    text = (
        f"wave land-batch integrated={landed_count} "
        f"close_mode={payload['close_mode']} attempts={attempts} "
        f"failed_at={batch_result['failed_wave']} error={batch_result['error']!r}"
    )
    emit_json_or_text(payload, text, flags=flags)
    raise typer.Exit(cli_errors.ValidationError.exit_code)


@wave_app.command(name="autoland")
def wave_autoland_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option(
            "--iter",
            help=("Scope the land to closed waves in this iter id; defaults to the current iter."),
        ),
    ] = None,
    keep_worktree: Annotated[
        bool,
        typer.Option(
            "--keep-worktree",
            help="Skip the post-land worktree teardown for each landed wave.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the planned land order without cherry-picking anything.",
        ),
    ] = False,
) -> None:
    """Cherry-pick closed waves' worktree commits home in dependency order.

    Lands every closed wave whose worktree branch still carries un-landed
    commits, deps before dependents (ties by wave id). Stops on the first
    cherry-pick conflict, leaving the repo conflicted for the operator and
    exiting STATE_CONFLICT; exits 0 once every landable wave is home.
    """
    flags: GlobalFlags = ctx.obj
    if iter_flag is not None and not is_iter_id(iter_flag):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_flag!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = _resolve_state_path(flags)
        repo_root = _resolve_repo_root(state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    try:
        result = _call_worktree_daemon(
            method="state.wave_autoland",
            params={
                "repo_root": str(repo_root),
                "iter_id": iter_flag,
                "keep_worktree": keep_worktree,
                "dry_run": dry_run,
            },
            flags=flags,
            verb="wave autoland",
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload: dict[str, Any] = {
        "order": list(result["order"]),
        "landed": [
            {
                "wave": r["wave"],
                "commits": list(r["commits"]),
                "merged_commit": r["merged_commit"],
                "worktree_cleaned": r["worktree_cleaned"],
            }
            for r in result["landed"]
        ],
        "failed_wave": result["failed_wave"],
        "error": result["error"],
        "remaining": list(result["remaining"]),
        "dry_run": result["dry_run"],
    }

    if result["dry_run"]:
        text = f"wave autoland dry-run order={payload['order']}"
        emit_json_or_text(payload, text, flags=flags)
        return

    landed_count = len(payload["landed"])
    if result["failed_wave"] is None:
        text = f"wave autoland landed={landed_count} order={payload['order']}"
        emit_json_or_text(payload, text, flags=flags)
        return

    # Stopped land. merge_back left the cherry-pick mid-flight and marked
    # the worktree record CONFLICTED; the landed prefix is already
    # persisted. The envelope carries the failing wave and the un-landed
    # remainder so the operator can resolve and re-run. STATE_CONFLICT (3)
    # mirrors how the underlying cherry-pick conflict surfaces.
    text = (
        f"wave autoland landed={landed_count} failed_at={result['failed_wave']} "
        f"remaining={payload['remaining']} error={result['error']!r}"
    )
    emit_json_or_text(payload, text, flags=flags)
    raise typer.Exit(cli_errors.StateConflict.exit_code)


__all__ = [
    "worktree_app",
]
