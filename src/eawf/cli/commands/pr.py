"""``eawf pr render <phase-id>`` Typer sub-app.

Read-only renderer. Loads state, projects through
:func:`eawf.render.pr_body.build_pr_body`, emits Markdown body (default) or
JSON envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.render.pr_body import PrBodyNotFound, build_pr_body
from eawf.state.ids import is_phase_id
from eawf.state.models import State
from eawf.state.resolve import resolve_with_reason
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)


pr_app = typer.Typer(
    name="pr",
    help="Render a phase PR body from state.json.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@pr_app.command("render")
def pr_render(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase id (e.g. P11).")],
) -> None:
    """Render the PR body for a phase as Markdown (or JSON envelope)."""
    flags: GlobalFlags = ctx.obj
    try:
        if not is_phase_id(phase_id):
            raise cli_errors.InvalidInput(f"invalid phase id: {phase_id!r} (expected P<NN>)")
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        try:
            body = build_pr_body(state, phase_id)
        except PrBodyNotFound as exc:
            raise cli_errors.NotFound(str(exc)) from exc
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "phase": phase_id,
        "body": body,
    }
    emit_json_or_text(payload, body, flags=flags)
