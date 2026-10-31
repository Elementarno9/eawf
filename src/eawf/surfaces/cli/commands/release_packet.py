"""``eawf release packet-run``: run one perfect-realization packet and file its receipt.

The receipt is machine-local evidence the stable checkpoint's ``packet_run``
assertion reads, not a record the daemon owns, so the verb runs in this
process like ``preflight``. The packet itself drives its own daemon in the
scratch repository. The logic lives in
:mod:`eawf.workflow.release.packet_producer`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.release import release_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.workflow.release.packet_producer import DEFAULT_PACKET_BUDGET, LiveRuntime, run_packet
from eawf.workflow.release.packet_run import PacketOrigin, PacketRuntimeId


@release_app.command("packet-run")
def release_packet_run(
    ctx: typer.Context,
    runtime: Annotated[
        PacketRuntimeId, typer.Option("--runtime", help="Runtime to dispatch the Task to.")
    ],
    origin: Annotated[
        PacketOrigin,
        typer.Option("--origin", help="born_native: eawf first; adopted: a project with history."),
    ],
    scratch: Annotated[
        Path,
        typer.Option("--scratch", help="Empty directory outside the checkout to run in."),
    ],
    budget: Annotated[
        int, typer.Option("--budget", min=1, help="Token ceiling of the dispatched Run.")
    ] = DEFAULT_PACKET_BUDGET,
) -> None:
    """Run the packet on a live runtime in a scratch repository and record the receipt.

    The packet initialises the scratch repository, plans one Task, certifies
    the runtime, dispatches the Task, lands its commit, closes the Batch and
    prints the Run's transcript. This spends the runtime's tokens.
    """
    flags: GlobalFlags = ctx.obj
    try:
        # The checkout root, as ``preflight`` reads it, so the receipt lands
        # where the release's ``packet_run`` assertion looks.
        receipt = run_packet(
            Path.cwd(), scratch, runtime=LiveRuntime(runtime), origin=origin, budget=budget
        )
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    failed = "" if receipt.failed_step is None else f" at {receipt.failed_step.value}"
    emit_json_or_text(
        receipt.model_dump(mode="json"),
        f"{runtime}/{origin.value} on {receipt.eawf_version}: {receipt.outcome}{failed} "
        f"({receipt.evidence_digest})",
        flags=flags,
    )
    if receipt.outcome == "fail":
        raise typer.Exit(code=1)


__all__ = ["release_packet_run"]
