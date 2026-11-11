"""``eawf worktree`` Typer sub-app: upkeep of the repository's linked worktrees.

CLI dispatch only: the handler resolves the main checkout, delegates the plan to
:mod:`eawf.runtime.workspace.worktree_gc` and formats it.

Verbs:

- ``eawf worktree gc --dry-run`` lists the reap candidates under
  ``.ea/worktrees/`` and prints the ``git worktree remove`` line of each
  landed tree for the operator; without ``--dry-run`` it refuses.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Final

import typer

from eawf.kernel.state.resolve import LinkedWorktreeStateError, resolve_with_reason
from eawf.runtime.vcs.branch_name import BareRepositoryError
from eawf.runtime.workspace.worktree_gc import NotARepositoryError, plan_worktree_gc
from eawf.surfaces.cli.errors import UserError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

#: The refusal of a reaping gc: removal waits for the registry of created trees.
GC_REFUSAL: Final = (
    "eawf worktree gc: reaping is not available before 0.8; "
    "run `eawf worktree gc --dry-run` and remove the listed trees yourself"
)

worktree_app = typer.Typer(
    name="worktree",
    help="Upkeep of the repository's linked worktrees.",
    no_args_is_help=True,
    add_completion=False,
)


@worktree_app.command("gc")
def worktree_gc(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List the candidates and remove nothing."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Shorthand for the global --json flag."),
    ] = False,
) -> None:
    """List the worktrees under ``.ea/worktrees/`` that a garbage collection may reap."""
    flags: GlobalFlags = ctx.obj
    if json_output:
        flags = replace(flags, json_output=True)
    if not dry_run:
        emit_error(UserError(GC_REFUSAL, kind="NotAvailable"), flags=flags)
    try:
        state_path, _reason = resolve_with_reason(workspace=flags.workspace)
        main_root = state_path.parent.parent.resolve()
        plan = plan_worktree_gc(main_root, now=datetime.now(UTC))
    except (LinkedWorktreeStateError, BareRepositoryError, NotARepositoryError) as exc:
        emit_error(UserError(str(exc), kind="NotFound"), flags=flags)
    candidates = [
        {
            "path": str(row.path),
            "reason": row.reason,
            "bytes": row.size_bytes,
            "remove": row.remove_line,
        }
        for row in plan.candidates
    ]
    kept = [
        {
            "path": str(tree.path),
            "flags": list(tree.flags),
            "ignored_files": tree.ignored_files,
            "ignored_bytes": tree.ignored_bytes,
        }
        for tree in plan.kept
    ]
    lines = [
        f"worktree gc (dry run): {len(candidates)} candidate(s), "
        f"integration branch {plan.integration or 'none'}"
    ]
    lines += [f"  {row.reason:<12} {row.size_bytes:>14,} B  {row.path}" for row in plan.candidates]
    lines += [
        f"  kept ({', '.join(tree.flags)}): {tree.path}"
        + (
            f" ({tree.ignored_files} ignored, {tree.ignored_bytes:,} B)"
            if tree.ignored_files
            else ""
        )
        for tree in plan.kept
    ]
    removes = [row.remove_line for row in plan.candidates if row.remove_line is not None]
    if removes:
        lines += ["run from the repository to remove each landed, clean tree:", *removes]
    emit_json_or_text(
        {
            "dry_run": True,
            "integration": plan.integration,
            "candidates": candidates,
            "kept": kept,
        },
        "\n".join(lines),
        flags=flags,
    )
