"""``eawf release candidate``: freeze the manifest and pin the checkpoint.

Split out of :mod:`eawf.surfaces.cli.commands.release`, whose
:data:`~eawf.surfaces.cli.commands.release.release_app` and daemon
dispatch this verb reuses, the same way the two train-walking verbs are.

It is the step between opening a checkpoint and approving it: the
artifact set an approval binds stops being assembled by hand and becomes
a derivation from the receipts the tag's publish jobs left behind. Like
``receipts`` and ``advance`` it takes no record file, because the record
it moves is the stored one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.release import (
    RELEASE_RPC_METHODS,
    _dispatch,
    _record_line,
    release_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


@release_app.command("candidate")
def release_candidate(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Checkpoint version to pin, e.g. 0.7.0.dev3.")],
    receipts_dir: Annotated[
        Path,
        typer.Option("--receipts", help="Directory the publication receipts were downloaded into."),
    ],
    source: Annotated[
        str, typer.Option("--source", help="Commit the published artifacts were built from.")
    ],
    manifest_ref: Annotated[
        str | None,
        typer.Option("--manifest-ref", help="Where the frozen manifest is stored."),
    ] = None,
    manifest_out: Annotated[
        Path | None,
        typer.Option("--manifest-out", help="Write the frozen manifest document to this path."),
    ] = None,
) -> None:
    """Freeze the manifest from the receipts and record the CANDIDATE.

    The artifact set an approval binds used to be assembled by hand. Here
    it is derived: one receipt per declared target, every declared
    artifact kind resolved to a file that receipt reported, and the
    registry identities read off the checkpoint configuration rather than
    spelled again. A directory missing a target, or holding receipts for
    another version, refuses and records nothing.

    The pinned tree is read from this checkout at ``--source``, so the
    commit and the tree cannot disagree, and the whole pin lands in one
    transition under the ``manifest_complete`` guard.

    ``--manifest-out`` saves the frozen manifest beside the receipts.
    Keep it: ``eawf release observe --manifest`` compares the registry
    against this exact document, and only the document whose digest the
    record pins is accepted there.
    """
    flags: GlobalFlags = ctx.obj
    try:
        result = _dispatch(
            RELEASE_RPC_METHODS["candidate"],
            {
                "version": version,
                "receipts_dir": str(receipts_dir),
                "source_sha": source,
                "manifest_ref": manifest_ref,
            },
        )
        if manifest_out is not None:
            manifest_out.parent.mkdir(parents=True, exist_ok=True)
            manifest_out.write_bytes(
                orjson.dumps(result.get("manifest"), option=orjson.OPT_INDENT_2)
            )
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot write the frozen manifest: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    saved = "" if manifest_out is None else f"\n  manifest saved: {manifest_out}"
    record = result.get("release") or {}
    text = (
        f"{_record_line(result)}\n"
        f"  record: {result.get('release_record_id')}\n"
        f"  source: {record.get('source_sha')} tree {result.get('source_tree_sha')}\n"
        f"  manifest: {result.get('manifest_digest')}\n"
        f"  supersedes: {result.get('supersedes_release_ref') or '(nothing)'}{saved}"
    )
    emit_json_or_text(result, text, flags=flags)


__all__ = ["release_candidate"]
