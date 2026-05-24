"""``eawf coauthor`` Typer sub-app."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


coauthor_app = typer.Typer(
    name="coauthor",
    help="Resolve co-author trailers from VCS config.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_anchors(flags: GlobalFlags) -> tuple[Path, Path | None]:
    return Path.cwd(), flags.workspace


@coauthor_app.command("resolve")
def coauthor_resolve(
    ctx: typer.Context,
    runtime: Annotated[
        str | None,
        typer.Option("--runtime", help="Runtime id to resolve, e.g. claude or codex."),
    ] = None,
    message_file: Annotated[
        Path | None,
        typer.Option("--message-file", help="Optional text file checked against disabled mode."),
    ] = None,
) -> None:
    """Resolve the configured co-author trailer."""
    from pydantic import ValidationError

    from eawf.kernel.config.layered import merge_config
    from eawf.runtime.vcs.coauthor import CoauthorPolicyError, VcsConfig, resolve_coauthor_trailer

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)
    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
        vcs = VcsConfig.model_validate(merged.get("vcs", {}))
        message_text = None
        if message_file is not None:
            try:
                message_text = message_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise cli_errors.UserError(
                    f"cannot read message file: {exc}", kind="InvalidInput"
                ) from exc
        trailer = resolve_coauthor_trailer(
            vcs.coauthor,
            runtime=runtime,
            env=os.environ,
            message_text=message_text,
        )
    except ValidationError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"vcs.coauthor schema rejected: {exc}"),
            flags=flags,
        )
        return
    except CoauthorPolicyError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    payload = {
        "mode": vcs.coauthor.mode,
        "runtime": runtime or vcs.coauthor.default_runtime,
        "trailer": trailer,
        "required": vcs.coauthor.require_trailer and vcs.coauthor.mode != "disabled",
    }
    text = "coauthor disabled" if trailer is None else trailer
    emit_json_or_text(payload, text, flags=flags)


__all__ = ["coauthor_app"]
