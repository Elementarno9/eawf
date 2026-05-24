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
:mod:`eawf.cli.commands.lifecycle`):

- ``wave land`` — cherry-pick a wave's worktree commits onto the
  parent branch, then close the wave with the resulting SHA. This is
  the AGENTS.md-discipline-compliant entry point (cherry-pick only,
  never merge).
- ``wave land-batch`` — apply ``wave land`` to every eligible wave in
  dep order; stop on the first failure.

Every mutating handler runs inside
:func:`eawf.cli._mutation.state_transaction` (state-side serialisation)
*and* :func:`eawf.worktree.locks.worktree_registry_lock` (git-side
registry serialisation). The two locks compose without re-entry: the
state lock guards ``state.json`` and the registry lock guards
``.git/worktrees/<name>``; they target disjoint paths.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.lifecycle import wave_app
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.ids import is_iter_id, is_wave_id

logger = logging.getLogger(__name__)

#: Merge-back strategy tokens — mirror
#: :data:`eawf.worktree.merge_back.STRATEGY_CHERRY_PICK` /
#: ``STRATEGY_REBASE_THEN_FF`` by value so the ``worktree merge-back
#: --strategy`` default + help text do not import the heavy ``eawf.worktree``
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
    from eawf.worktree.git import repo_root as resolve_repo_root

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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import create_worktree, worktree_registry_lock

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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import list_worktrees

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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import merge_back, worktree_registry_lock

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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import worktree_registry_lock

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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import cleanup_worktree, worktree_registry_lock

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
) -> None:
    """Cherry-pick the wave's worktree commits onto the parent branch.

    Always uses cherry-pick (per AGENTS.md rule 11). On conflict, the
    wave is *not* closed and the on-disk repo state is preserved so the
    operator can resolve and re-run.
    """
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import wave_land, worktree_registry_lock

    flags: GlobalFlags = ctx.obj
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

    result = None
    # Lock ordering invariant: registry → state (see module docstring).
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            result = wave_land(
                state,
                repo_root=repo_root,
                wave_id=wave_id,
                outcome=outcome,
                keep_worktree=keep_worktree,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    assert result is not None
    payload: dict[str, Any] = {
        "wave": result.wave_id,
        "commits": list(result.commits),
        "outcome": result.outcome,
        "worktree_cleaned": result.worktree_cleaned,
        "merged_commit": result.merged_commit,
    }
    text = (
        f"wave land {wave_id} commits={result.commits} "
        f"outcome={result.outcome!r} cleaned={result.worktree_cleaned}"
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
            help="Skip waves whose declared deps are not all CLOSED.",
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
    from eawf.cli._mutation import state_transaction
    from eawf.worktree import wave_land_batch, worktree_registry_lock

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

    batch_result = None
    try:
        with (
            worktree_registry_lock(repo_root, timeout=5.0),
            state_transaction(state_path) as state,
        ):
            batch_result = wave_land_batch(
                state,
                repo_root=repo_root,
                iter_id=iter_flag,
                ready_only=ready_only,
                keep_worktree=keep_worktree,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    assert batch_result is not None
    landed_payload = [
        {
            "wave": r.wave_id,
            "commits": list(r.commits),
            "outcome": r.outcome,
            "worktree_cleaned": r.worktree_cleaned,
            "merged_commit": r.merged_commit,
        }
        for r in batch_result.landed
    ]
    payload: dict[str, Any] = {
        "landed": landed_payload,
        "failed_wave": batch_result.failed_wave,
        "error": batch_result.error,
        "skipped": list(batch_result.skipped),
    }
    landed_count = len(batch_result.landed)
    if batch_result.failed_wave is None:
        text = f"wave land-batch landed={landed_count} skipped={batch_result.skipped}"
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
        f"wave land-batch landed={landed_count} failed_at={batch_result.failed_wave} "
        f"error={batch_result.error!r}"
    )
    emit_json_or_text(payload, text, flags=flags)
    raise typer.Exit(cli_errors.ValidationError.exit_code)


__all__ = [
    "worktree_app",
]
