"""``eawf run report``: write the plain-text report of one Run under ``.ea/local/``.

The verb is the command-line twin of the console's export card. It is a local file write
with no canonical effect: it prints its consequence block -- parts, sizes, redactions,
destination -- before it writes, then answers with the path. Nothing leaves the machine.

Importing this module attaches the verb to the ``run`` group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import run_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path


@run_app.command("report")
def run_report_cmd(
    ctx: typer.Context,
    run: Annotated[str, typer.Argument(help="The Run's key or URN.")],
    parts: Annotated[
        str | None,
        typer.Option(
            "--parts",
            help=(
                "Comma-separated parts: timeline, usage_and_cost, transcript, secrets,"
                " sandbox_decisions. Secrets are never included."
            ),
        ),
    ] = None,
    actor: Annotated[
        str | None, typer.Option("--actor", help="Principal key the report is authored by.")
    ] = None,
) -> None:
    """Write the plain-text report of one Run; no record moves."""
    from eawf.observability.reflect.run_report import (
        parse_parts,
        plan_run_report,
        write_run_report,
    )
    from eawf.observability.reflect.runs import read_tree_run

    flags: GlobalFlags = ctx.obj
    try:
        asked = parse_parts(parts)
        tree_root = resolve_state_path(flags.workspace).parent
        reading = read_tree_run(tree_root, run)
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
    except (FileNotFoundError, LookupError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
    plan = plan_run_report(
        reading, parts=asked, actor=actor, tree_root=tree_root, on=datetime.now(UTC).date()
    )
    typer.echo("\n".join(plan.consequence_lines()), err=True)
    path = write_run_report(plan)
    emit_json_or_text(
        {
            "path": str(path),
            "parts": {part.name.value: part.size for part in plan.parts},
            "redactions": plan.redactions,
        },
        str(path),
        flags=flags,
    )


__all__ = ["run_report_cmd"]
