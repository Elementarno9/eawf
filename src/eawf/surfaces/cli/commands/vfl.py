"""``eawf vfl`` Typer sub-app -- visual-fidelity-layer golden management.

CLI dispatch only (AGENTS rule 1): the ``approve`` handler parses args,
resolves a snapshot surface from the locked inventory, and reuses the
:func:`eawf.surfaces.cli.commands.snapshot.run_regen` library helper to
rewrite the surface's committed golden bytes -- exactly the regeneration
path ``eawf snapshot update`` drives, so the two surfaces never diverge.

``eawf vfl approve`` exists for the SVG visual-fidelity oracle workflow
(FS17): after a deliberate render change, the operator runs ``approve`` on
a renderer host (e.g. one with ``resvg``) to bless the new golden bytes,
then commits them under a wave-form ``[P##-W##] test:`` subject so the CI
snapshot-pairing gate accepts the mutation.

The verifiable guard: ``approve`` refuses a no-op. The handler regenerates
the surface in place, then inspects the working-tree diff scoped to the
surface's ``golden_dir``. An empty diff means the committed golden already
matched current-code output -- there is nothing to approve -- so the verb
exits non-zero. A non-empty diff means the regeneration produced new bytes;
the verb exits ``0`` having staged the approval for the operator to commit.

Verbs:

- ``eawf vfl approve --kind <surface>`` -- regenerate + approve one
  surface's golden bytes (refuses when there is no pending diff).

Exit codes:

- ``0`` -- a pending golden diff was approved (regeneration changed bytes).
- ``1`` (``USER_ERROR``) -- unknown ``--kind``, regeneration failed, or
  there was no pending golden diff to approve.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.snapshot import (
    SnapshotSurface,
    resolve_surface,
    run_regen,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


vfl_app = typer.Typer(
    name="vfl",
    help="Visual-fidelity-layer golden management -- approve regenerated goldens.",
    no_args_is_help=True,
    add_completion=False,
)


def golden_dir_has_diff(surface: SnapshotSurface, *, workspace: Path | None) -> bool:
    """Return whether the working tree has uncommitted changes under *surface*'s goldens.

    Runs ``git diff --quiet -- <golden_dir>`` (which compares the working
    tree against the index + HEAD): exit ``0`` means no change, exit ``1``
    means there is a pending diff. The check is scoped to the surface's
    ``golden_dir`` so an unrelated edit elsewhere never reads as a pending
    golden approval.

    Args:
        surface: The resolved snapshot surface whose ``golden_dir`` is
            inspected.
        workspace: Optional repo root the ``git diff`` runs in (the
            subprocess cwd); ``None`` keeps the current directory.

    Returns:
        ``True`` when ``git diff`` reports changes under the golden dir,
        ``False`` when the golden tree is clean.
    """
    cwd = str(workspace) if workspace is not None else None
    completed = subprocess.run(
        ["git", "diff", "--quiet", "--", surface.golden_dir],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    has_diff = completed.returncode != 0
    logger.info(
        f"golden_dir_has_diff kind={surface.kind} dir={surface.golden_dir!r} diff={has_diff}"
    )
    return has_diff


@vfl_app.command("approve")
def vfl_approve(
    ctx: typer.Context,
    kind: Annotated[
        str,
        typer.Option("--kind", help="Snapshot surface to approve (see `eawf snapshot list`)."),
    ],
) -> None:
    """Regenerate and approve one surface's golden bytes.

    The surface's committed golden bytes are rewritten in place via the
    shared snapshot regeneration helper. The verb then inspects the
    working-tree diff scoped to the surface's ``golden_dir``: a non-empty
    diff means the regeneration produced a pending approval, so the verb
    exits ``0`` and the operator commits the bytes as
    ``[P<NN>-W<NN>] test: snapshot update <kind>``. An empty diff means
    the golden already matched current-code output -- there is nothing to
    approve -- so the verb refuses with ``USER_ERROR``.

    Exits ``1`` (``USER_ERROR``) when ``--kind`` is unknown, the
    regeneration subprocess fails, or there is no pending golden diff to
    approve.
    """
    flags: GlobalFlags = ctx.obj
    try:
        surface = resolve_surface(kind)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    completed = run_regen(surface, workspace=flags.workspace, output_dir=None)
    if completed.returncode != 0:
        # Surface the regeneration tail so the operator sees the failing
        # pytest node without re-running it manually.
        tail = (completed.stdout or completed.stderr or "").strip().splitlines()[-20:]
        sys.stderr.write("\n".join(tail) + "\n")
        cli_errors.emit_error(
            cli_errors.UserError(
                f"golden regeneration failed for kind {surface.kind!r} "
                f"(exit {completed.returncode}); see output above"
            ),
            flags=flags,
            data={"kind_surface": surface.kind, "exit_code": completed.returncode},
        )
        return

    if not golden_dir_has_diff(surface, workspace=flags.workspace):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"nothing to approve for kind {surface.kind!r}: "
                f"golden under {surface.golden_dir!r} already matches current output"
            ),
            flags=flags,
            data={"kind_surface": surface.kind},
        )
        return

    payload: dict[str, object] = {
        "kind": surface.kind,
        "golden_dir": surface.golden_dir,
        "approved": True,
    }
    text = (
        f"vfl approve: regenerated + approved {surface.kind} -> {surface.golden_dir}\n"
        f"commit the bytes as `[P<NN>-W<NN>] test: snapshot update {surface.kind}`"
    )
    emit_json_or_text(payload, text, flags=flags)
