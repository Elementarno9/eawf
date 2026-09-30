"""``eawf run interrupt``, ``cancel`` and ``reconcile``: ask a Run for a control.

``eawf run pause-dispatch``, ``drain-dispatch`` and ``resume-dispatch`` ask the daemon's
scheduler instead: a pause or a drain holds the admission of every new Run and stops or
cancels nothing already claimed, a resume admits again, and a drain is refused while a
release is publishing. They are the command-line twins of the unattended route's requests.

The command-line twin of the console's Run controls. Each records that a principal asked
for the control; the Run moves only when it answers, so the command's answer is the
disposition of the control fact that now stands, never the Run's new state. Each prints
its consequence block before it sends; ``--dry-run`` prints only the block and sends
nothing. The request is filed under its idempotency key, so asking again under the same
key answers the first request rather than making a second.

Importing this module attaches the verbs to the ``run`` group.
"""

from __future__ import annotations

from typing import Annotated, Final

import typer

from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.lifecycle import run_app
from eawf.surfaces.cli.commands.question_decision import forward_answer
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verb that records a principal's request for a Run control.
RUN_CONTROL_REQUEST: Final = "runtime.run.control.request"

#: The daemon verb that records a pause, drain or resume of the dispatch scheduler.
DISPATCH_CONTROL_REQUEST: Final = "runtime.dispatch.control.request"

#: What a dispatch request is about: the tree's one queue, which has no URN.
DISPATCH_QUEUE: Final = "dispatch-queue"

_Urn = Annotated[str, typer.Argument(help="The Run to ask.")]
_Key = Annotated[
    str,
    typer.Option(
        "--idempotency-key",
        help="The control request's own reference, CTL-<id>; a retry under it writes nothing.",
    ),
]
_Actor = Annotated[str, typer.Option("--actor", help="Principal key asking for the control.")]


def _request(
    ctx: typer.Context,
    control: str,
    *,
    urn: str,
    idempotency_key: str,
    actor: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Print the consequence of one Run control request, then send it unless told not to."""
    flags: GlobalFlags = ctx.obj
    if not preview(RUN_CONTROL_REQUEST, urn, None, flags=flags, dry_run=dry_run, yes=yes):
        return
    params = {
        "urn": urn,
        "control_request_ref": idempotency_key,
        "control": control,
        "actor": actor,
    }
    forward_answer(RUN_CONTROL_REQUEST, params, urn=urn, verb_text=f"run {control}", flags=flags)


@run_app.command("interrupt")
def run_interrupt_cmd(
    ctx: typer.Context,
    urn: _Urn,
    idempotency_key: _Key,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask a Run to stop at its next safe point; it keeps its work."""
    _request(
        ctx,
        "interrupt",
        urn=urn,
        idempotency_key=idempotency_key,
        actor=actor,
        dry_run=dry_run,
        yes=yes,
    )


@run_app.command("cancel")
def run_cancel_cmd(
    ctx: typer.Context,
    urn: _Urn,
    idempotency_key: _Key,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask a Run to end without finishing its work."""
    _request(
        ctx,
        "cancel",
        urn=urn,
        idempotency_key=idempotency_key,
        actor=actor,
        dry_run=dry_run,
        yes=yes,
    )


@run_app.command("reconcile")
def run_reconcile_cmd(
    ctx: typer.Context,
    urn: _Urn,
    idempotency_key: _Key,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask a Run to reconcile a control whose outcome is unknown."""
    _request(
        ctx,
        "reconcile",
        urn=urn,
        idempotency_key=idempotency_key,
        actor=actor,
        dry_run=dry_run,
        yes=yes,
    )


def _dispatch(
    ctx: typer.Context, verb: str, *, idempotency_key: str, actor: str, dry_run: bool, yes: bool
) -> None:
    """Print the consequence of one dispatch request, then send it unless told not to."""
    flags: GlobalFlags = ctx.obj
    method = DISPATCH_CONTROL_REQUEST
    if not preview(method, DISPATCH_QUEUE, None, flags=flags, dry_run=dry_run, yes=yes):
        return
    params = {"verb": verb, "actor": actor, "request_ref": idempotency_key}
    forward_answer(
        method, params, urn=DISPATCH_QUEUE, verb_text=f"run {verb}-dispatch", flags=flags
    )


_DispatchKey = Annotated[
    str,
    typer.Option(
        "--idempotency-key",
        help="The dispatch request's own reference, DSP-<id>; a retry under it writes nothing.",
    ),
]


@run_app.command("pause-dispatch")
def run_pause_dispatch_cmd(
    ctx: typer.Context,
    idempotency_key: _DispatchKey,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask the scheduler to admit no new Run until a resume; claimed Runs go on."""
    _dispatch(ctx, "pause", idempotency_key=idempotency_key, actor=actor, dry_run=dry_run, yes=yes)


@run_app.command("drain-dispatch")
def run_drain_dispatch_cmd(
    ctx: typer.Context,
    idempotency_key: _DispatchKey,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask the scheduler to start nothing new and let what runs finish; cancels nothing."""
    _dispatch(ctx, "drain", idempotency_key=idempotency_key, actor=actor, dry_run=dry_run, yes=yes)


@run_app.command("resume-dispatch")
def run_resume_dispatch_cmd(
    ctx: typer.Context,
    idempotency_key: _DispatchKey,
    actor: _Actor,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Ask the scheduler to admit queued Runs again after a pause or a drain."""
    _dispatch(ctx, "resume", idempotency_key=idempotency_key, actor=actor, dry_run=dry_run, yes=yes)


__all__ = [
    "DISPATCH_CONTROL_REQUEST",
    "RUN_CONTROL_REQUEST",
    "run_cancel_cmd",
    "run_drain_dispatch_cmd",
    "run_interrupt_cmd",
    "run_pause_dispatch_cmd",
    "run_reconcile_cmd",
    "run_resume_dispatch_cmd",
]
