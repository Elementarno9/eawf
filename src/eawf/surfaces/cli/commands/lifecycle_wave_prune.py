"""``eawf wave prune-branches`` -- delete local heads an archive ref already preserves.

Split out of :mod:`eawf.surfaces.cli.commands.lifecycle_wave_read`, which sits
near the repo's per-module line cap. This module owns only the prune verb; it
attaches to the shared ``wave_app`` Typer app defined in
:mod:`eawf.surfaces.cli.commands.lifecycle` the same way its siblings do.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import _resolve_repo_root_for_drift, wave_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


@wave_app.command("prune-branches")
def wave_prune_branches_cmd(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print exactly what would be deleted or pruned without doing it.",
        ),
    ] = False,
) -> None:
    """Delete local branch heads that ``wave archive-refs`` has already preserved.

    A branch is deleted only when an archive ref under ``refs/eawf/archive``
    points at exactly its current head. If ANY candidate head lacks that
    match -- unarchived, or archived at a different commit -- the whole
    batch is refused and nothing is deleted; run ``wave archive-refs``
    first (add ``--include-misc`` to cover non-wave branches too).
    ``main``, ``plugins-dist``, the current branch, and any branch checked
    out in another worktree are never candidates; they are reported under
    ``skipped``. Also removes worktree registrations whose directory no
    longer exists on disk (the outcome ``git worktree prune`` produces).

    ``--dry-run`` computes the same selection and worktree-prune list and
    prints it, without deleting or pruning anything.
    """
    from eawf.workflow.lifecycle.wave_archive import (
        WaveArchiveError,
        WaveBranchPruneRefusedError,
        prune_branches,
    )

    flags: GlobalFlags = ctx.obj
    repo_root = _resolve_repo_root_for_drift(flags.workspace)
    try:
        result = prune_branches(repo_root=repo_root, dry_run=dry_run)
    except WaveBranchPruneRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="ArchiveMissing"), flags=flags)
        return
    except WaveArchiveError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(str(exc), kind="ArchiveUnavailable"), flags=flags
        )
        return

    verb = "would delete" if dry_run else "deleted"
    worktree_verb = "would prune" if dry_run else "pruned"
    payload: dict[str, Any] = {
        "dry_run": result.dry_run,
        "deleted_count": len(result.deleted),
        "deleted": [
            {"branch": c.branch, "commit": c.head, "archive_ref": c.archive_ref}
            for c in result.deleted
        ],
        "skipped": [{"branch": branch, "reason": reason} for branch, reason in result.skipped],
        "pruned_worktrees": result.pruned_worktrees,
    }
    lines = [
        f"wave prune-branches: {len(result.deleted)} {verb}, "
        f"{len(result.skipped)} skipped, "
        f"{len(result.pruned_worktrees)} worktree(s) {worktree_verb}"
    ]
    lines.extend(f"  {verb} {c.branch} ({c.head[:12]})" for c in result.deleted)
    lines.extend(f"  skipped {branch} ({reason})" for branch, reason in result.skipped)
    lines.extend(f"  {worktree_verb} worktree {path}" for path in result.pruned_worktrees)
    emit_json_or_text(payload, "\n".join(lines), flags=flags)
