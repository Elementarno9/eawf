"""``eawf run dispatch``: run one Task as a new Run on a provider.

A dispatch over ``runtime.task.dispatch``. The daemon reads the Task, the
provider preference and the installed certifications, claims and starts the
Task, creates its Run and answers once the worker is announced; this command
sends the Task key and prints the answer. An omitted ``--provider`` is left
for the daemon to resolve from ``runtime.preference``.

Importing this module attaches the verb to the ``run`` group.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

import typer

from eawf.surfaces.cli import errors
from eawf.surfaces.cli.commands.lifecycle import run_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

#: The daemon verb this command forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
TASK_DISPATCH: Final = "runtime.task.dispatch"


@run_app.command("dispatch")
def run_dispatch_cmd(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="The Task's key or URN.")],
    actor: Annotated[str, typer.Option("--actor", help="Principal key dispatching the Task.")],
    provider: Annotated[
        Literal["claude-code", "codex"] | None,
        typer.Option("--provider", help="Runtime to run on; runtime.preference otherwise."),
    ] = None,
    budget: Annotated[
        int | None, typer.Option("--budget", min=1, help="The Run's token ceiling.")
    ] = None,
) -> None:
    """Run a planned Task as a new Run; answers once its worker is announced."""
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer

    flags: GlobalFlags = ctx.obj
    params: dict[str, Any] = {"task": task, "actor": actor}
    if provider is not None:
        params["provider"] = provider
    if budget is not None:
        params["budget"] = budget
    try:
        answer = _native_answer(TASK_DISPATCH, params, flags=flags, verb_text="run dispatch")
    except DaemonRpcError as exc:
        errors.emit_error(errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
        return
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    emit_json_or_text(
        answer,
        f"dispatched {answer['task_key']} as {answer['run_key']} "
        f"(attempt {answer['attempt_ref']}, stage {answer['stage']})",
        flags=flags,
    )


__all__ = ["TASK_DISPATCH", "run_dispatch_cmd"]
