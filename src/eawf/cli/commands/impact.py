"""``eawf impact [--decision=DXX] [--format=text|dot]`` CLI surface."""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.kernel.state.resolve import resolve_with_reason

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


class _ImpactFormat(StrEnum):
    TEXT = "text"
    DOT = "dot"


def _load_state(state_path: Path) -> State:
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


def impact_cmd(
    ctx: typer.Context,
    decision_id: Annotated[
        str | None,
        typer.Option(
            "--decision",
            help="Filter to a single decision id (e.g. D01).",
        ),
    ] = None,
    fmt: Annotated[
        _ImpactFormat,
        typer.Option(
            "--format",
            help="Output format (text or dot).",
        ),
    ] = _ImpactFormat.TEXT,
) -> None:
    """Render the decision → wave → file-glob impact graph."""
    from eawf.render.impact import build_impact_graph, render_dot, render_text

    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        graph = build_impact_graph(state, decision_id=decision_id)
        body = render_text(graph) if fmt is _ImpactFormat.TEXT else render_dot(graph)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "format": fmt.value,
        "decision": decision_id,
        "nodes": [n.model_dump() for n in graph.nodes],
        "body": body,
    }
    emit_json_or_text(payload, body, flags=flags)
