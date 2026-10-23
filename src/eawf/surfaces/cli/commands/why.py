"""``eawf why`` provenance surface.

Explains one native decision of the tree's selected generation: what was
decided and why, the scope it governs, the evidence and questions it cites
and its supersession links. An epoch-1 tree selects no generation, so it is
refused as needing migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.observability.reflect.decisions import WhyResult


def why_cmd(
    ctx: typer.Context,
    urn: Annotated[
        str,
        typer.Argument(help="Decision key, or an address ending in /decision/<key>."),
    ],
) -> None:
    """Explain why a native decision stands as it does."""
    from eawf.kernel.state.epoch2.authority import resolve_authority
    from eawf.observability.reflect.decisions import explain_decision, read_native_decisions
    from eawf.surfaces.cli.flag_day_gate import MIGRATION_REQUIRED_KIND, MIGRATION_VERB

    flags: GlobalFlags = ctx.obj
    try:
        tree = resolve_state_path(flags.workspace).parent
        if resolve_authority(tree).epoch != 2:
            raise cli_errors.MigrationRequired(
                f"{tree} is an epoch-1 tree and why explains only the decisions of an "
                f"epoch-2 generation; migrate it with `{MIGRATION_VERB} --plan` then `--apply`",
                kind=MIGRATION_REQUIRED_KIND,
            )
        result = explain_decision(read_native_decisions(tree), urn)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
    except KeyError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)

    emit_json_or_text(result.model_dump(mode="json"), _render_why(result), flags=flags)


def _render_why(result: WhyResult) -> str:
    lines = [
        f"{result.kind}: {result.id} ({result.status})",
        f"title: {result.title}",
        f"summary: {result.summary}",
    ]
    if result.refs:
        lines.append("refs:")
        lines.extend(f"- {ref.kind}: {ref.ref}" for ref in result.refs)
    else:
        lines.append("refs: none")
    return "\n".join(lines)


__all__ = ["why_cmd"]
