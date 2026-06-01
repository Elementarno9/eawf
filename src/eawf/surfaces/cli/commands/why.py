"""``eawf why`` provenance surface."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path


def why_cmd(
    ctx: typer.Context,
    urn: Annotated[
        str,
        typer.Argument(
            help=("Supported URN or bare id: phase, iter, wave, hypothesis, decision, or audit.")
        ),
    ],
) -> None:
    """Explain why an EAWF entity has its current trust tier."""
    from eawf.workflow.estimation.trust_scorecard import assemble_why, read_store_projection
    from eawf.workflow.evidence._io import load_state

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        state = load_state(state_path)
        projection = read_store_projection(state_path)
        result = assemble_why(state, urn, store_projection=projection)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except (KeyError, ValueError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return

    payload: dict[str, Any] = result.model_dump(mode="json")
    emit_json_or_text(payload, _render_why(result), flags=flags)


def _render_why(result: Any) -> str:
    lines = [
        f"{result.kind}: {result.id}",
        f"tier: {result.tier}",
        f"summary: {result.summary}",
    ]
    if result.refs:
        lines.append("refs:")
        for ref in result.refs:
            lines.append(f"- {ref.kind} {ref.tier}: {ref.summary} ({ref.urn})")
    else:
        lines.append("refs: none")
    return "\n".join(lines)


__all__ = ["why_cmd"]
