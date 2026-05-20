"""``eawf wiki render`` Typer sub-app.

Read-only renderer. Loads state, projects through
:func:`eawf.render.wiki.build_wiki`, emits Markdown (default) or JSON
envelope. Optional ``--output PATH`` writes the rendered Markdown to disk.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.state.resolve import resolve_with_reason

if TYPE_CHECKING:
    from eawf.state.models import State

logger = logging.getLogger(__name__)


wiki_app = typer.Typer(
    name="wiki",
    help="Render a per-phase narrative project wiki from state.json.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    from eawf.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@wiki_app.command("render")
def wiki_render(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Optional path to write the rendered Markdown.",
        ),
    ] = None,
) -> None:
    """Render the project wiki as Markdown (or JSON envelope)."""
    from eawf.render.wiki import build_wiki

    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        body = build_wiki(state)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")

    payload = {
        "body": body,
        "output": str(output) if output is not None else None,
        "h1_count": sum(1 for line in body.splitlines() if line.startswith("# ")),
    }
    emit_json_or_text(payload, body, flags=flags)
