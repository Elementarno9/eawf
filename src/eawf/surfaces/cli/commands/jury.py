"""``eawf jury`` Typer sub-app — the calibration gold-label writer.

One verb today:

* ``eawf jury label <wave-id> --good|--bad --reason TEXT`` — append an
  operator ground-truth label through the daemon's ``jury.label`` RPC
  (rule 4: the committed stores are daemon-written). The label feeds the
  jury-validation cohort that earns (or withholds) the jury's BLOCKING
  authority; the >= 20-char reason floor forces a real rationale.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


jury_app = typer.Typer(
    name="jury",
    help="Cross-vendor jury calibration surfaces (gold-label writer).",
    no_args_is_help=True,
    add_completion=False,
)


@jury_app.command("label")
def jury_label_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave id the label is about.")],
    good: Annotated[
        bool, typer.Option("--good", help="The wave was actually a good outcome.")
    ] = False,
    bad: Annotated[
        bool, typer.Option("--bad", help="The wave was actually a bad outcome.")
    ] = False,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why this label is pinned (>= 20 chars)."),
    ] = "",
) -> None:
    """Append an operator gold label for a wave via the daemon.

    Exactly one of ``--good`` / ``--bad`` is required. Labels are
    append-only: a re-labelled wave gets a fresh record and the latest
    wins, so a correction supersedes an earlier mistake without
    rewriting history.
    """
    flags: GlobalFlags = ctx.obj
    if good == bad:
        cli_errors.emit_error(
            cli_errors.UserError("pass exactly one of --good / --bad", kind="InvalidInput"),
            flags=flags,
        )
        return
    repo_root = (flags.workspace or Path.cwd()).resolve()
    params: dict[str, Any] = {
        "wave_id": wave_id,
        "ground_truth": good,
        "reason": reason,
        "repo_root": str(repo_root),
    }
    try:
        with DaemonClient() as client:
            result = client.call("jury.label", params)
    except DaemonRpcError as exc:
        if exc.code in (-32602, cli_errors.RPC_VALIDATION_FAILED):
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (OSError, RuntimeError, TimeoutError) as exc:
        raise cli_errors.DaemonUnreachable(f"daemon unavailable for jury.label: {exc}") from exc
    emit_json_or_text(
        result,
        f"jury label {result.get('wave_id')!r} ground_truth={result.get('ground_truth')}",
        flags=flags,
    )


__all__ = ["jury_app"]
