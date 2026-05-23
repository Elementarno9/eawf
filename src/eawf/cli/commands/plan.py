"""``eawf plan show`` — read-only iter plan view.

Renders the active iteration plan as either a deterministic markdown body
(human reviewers, GitHub PR previews) or a JSON envelope conforming to
``src/eawf/schemas/plan-view.schema.json`` (tooling).

Resolves the active iter (``state.current.iter_id``) when ``--iter`` is
omitted, then projects the validated :class:`~eawf.state.models.State`
through :func:`eawf.render.plan_view.build_view`. The handler is a pure
projection — read-only over ``state.json`` (rule 4: no lock acquisition,
no JSONL appends, no state mutations).

Exit codes:

- ``0`` on success.
- ``2`` (``NOT_FOUND``) when no ``state.json`` is found, OR when the
  resolved iter is not in ``state.iters``.
- ``3`` (``INVALID_INPUT``) when ``--iter`` fails the iter-id grammar,
  when no active iter is set and ``--iter`` is omitted, or when
  ``--json`` and ``--format markdown`` are passed together.
- ``4`` (``VALIDATION_FAILED``) when the resolved ``state.json`` fails
  Pydantic schema validation.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import click
import orjson
import typer

from eawf.cli import errors
from eawf.cli.commands.draft import install_promote_command
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.state.ids import is_iter_id
from eawf.state.resolve import resolve_with_reason

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class _PlanFormat(StrEnum):
    """Output format selector for ``--format``."""

    MARKDOWN = "markdown"
    JSON = "json"


class PlanSection(StrEnum):
    """Section selector for ``--show <section>``.

    Mirrors :class:`eawf.render.plan_view.PlanSection` by string value so
    the registered command can declare its ``--show`` choices without
    importing the heavy ``render.plan_view`` subtree (which pulls
    ``state.models``) at command-tree build time. ``StrEnum`` equality is
    by value, so members compare equal to the renderer's enum at call time.
    """

    ALL = "all"
    DAG = "dag"
    CHECKS = "checks"
    RISKS = "risks"
    WAVES = "waves"


plan_app = typer.Typer(
    name="plan",
    help="Read-only iter plan view (DAG, waves, checks, risks).",
    no_args_is_help=True,
    add_completion=False,
)

install_promote_command(plan_app, "plan")


@plan_app.command(name="show")
def show_cmd(
    ctx: typer.Context,
    iter_id: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID (defaults to state.current.iter_id)."),
    ] = None,
    fmt: Annotated[
        _PlanFormat,
        typer.Option(
            "--format",
            help="Output format (markdown or json).",
        ),
    ] = _PlanFormat.MARKDOWN,
    show: Annotated[
        PlanSection,
        typer.Option(
            "--show",
            help="Section selector (all/dag/checks/risks/waves).",
        ),
    ] = PlanSection.ALL,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root for state.json resolution (overrides pwd-upward).",
        ),
    ] = None,
    ascii_dag: Annotated[
        bool,
        typer.Option(
            "--ascii",
            help="Render the DAG as ASCII (markdown branch only).",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit JSON output (forces --format json).",
        ),
    ] = False,
    md_output: Annotated[
        bool,
        typer.Option("--md", help="Emit markdown output (alias for --format markdown)."),
    ] = False,
) -> None:
    """Print the active iter plan view (markdown or JSON)."""
    from pydantic import ValidationError

    from eawf.render.plan_view import (
        PlanSection as RenderPlanSection,
    )
    from eawf.render.plan_view import (
        PlanViewNotFound,
        build_view,
        render_json,
        render_markdown,
    )
    from eawf.state.models import State

    # The registered ``--show`` choices use the local :class:`PlanSection`
    # mirror so the command tree builds without importing the heavy
    # ``render.plan_view`` subtree. Map to the renderer's enum (same string
    # values) for the typed render calls below.
    render_section = RenderPlanSection(show.value)

    flags: GlobalFlags = ctx.obj
    parent_json = bool(flags.json_output)
    local_json = bool(json_output)
    json_requested = parent_json or local_json
    if md_output:
        fmt = _PlanFormat.MARKDOWN
    if md_output and json_requested:
        errors.emit_error(
            errors.UserError("--md and --json are contradictory", kind="InvalidInput"),
            flags=flags,
        )
        return

    # Format conflict: --json plus an *explicit* --format markdown is
    # contradictory. We only fire when the user actually typed --format
    # markdown (default values are silently consistent with --json).
    if json_requested and fmt is _PlanFormat.MARKDOWN:
        src = ctx.get_parameter_source("fmt")
        if src is click.core.ParameterSource.COMMANDLINE:
            errors.emit_error(
                errors.UserError(
                    "--json and --format markdown are contradictory", kind="InvalidInput"
                ),
                flags=flags,
            )
            return

    effective_flags = GlobalFlags(
        json_output=json_requested or fmt is _PlanFormat.JSON,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=workspace if workspace is not None else flags.workspace,
    )

    if iter_id is not None and not is_iter_id(iter_id):
        errors.emit_error(
            errors.UserError(
                f"invalid iter id: {iter_id!r} (expected P<NN>-I<NN>)", kind="InvalidInput"
            ),
            flags=effective_flags,
        )
        return

    state_path, _reason = resolve_with_reason(workspace=effective_flags.workspace)
    if not state_path.exists():
        errors.emit_error(
            errors.UserError(f"no state.json at {state_path}", kind="NotFound"),
            flags=effective_flags,
        )
        return

    try:
        payload_dict = orjson.loads(state_path.read_bytes())
        state = State.model_validate(payload_dict)
    except ValidationError as exc:
        errors.emit_error(
            errors.ValidationError(
                f"state file failed schema validation: {exc.errors()[0]['msg']}"
            ),
            flags=effective_flags,
        )
        return
    except orjson.JSONDecodeError as exc:
        errors.emit_error(
            errors.ValidationError(f"state file is not valid JSON: {exc}"),
            flags=effective_flags,
        )
        return

    resolved_iter_id = iter_id if iter_id is not None else state.current.iter_id
    if resolved_iter_id is None:
        errors.emit_error(
            errors.UserError(
                "no active iter set; pass --iter <ID> explicitly", kind="InvalidInput"
            ),
            flags=effective_flags,
        )
        return

    try:
        view = build_view(state, resolved_iter_id)
    except PlanViewNotFound as exc:
        errors.emit_error(
            errors.UserError(str(exc), kind="NotFound"),
            flags=effective_flags,
        )
        return

    if effective_flags.json_output:
        envelope: dict[str, Any] = render_json(view, sections=render_section)
        # emit_json_or_text honours flags.json_output; we pass a dummy text body
        # because the JSON branch never consumes it.
        emit_json_or_text(envelope, "<json>", flags=effective_flags)
        return

    body = render_markdown(view, ascii_dag=ascii_dag, sections=render_section)
    typer.echo(body, nl=False)
