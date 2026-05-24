"""Research store read commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.cli import errors
from eawf.cli.commands.draft import install_promote_command
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.enums import StoreKind

if TYPE_CHECKING:
    from eawf.kernel.store.envelope import Envelope

research_app = typer.Typer(
    name="research",
    help="Show and promote research briefs.",
    no_args_is_help=True,
)

install_promote_command(research_app, "research")


def _load_research_envelope(state_path: Path, record_id: str) -> Envelope:
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.paths import store_path

    path = store_path(state_path, StoreKind.RESEARCH)
    if not path.exists():
        raise errors.UserError("research store is empty", kind="NotFound")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate(orjson.loads(line))
        if envelope.id == record_id:
            return envelope
    raise errors.UserError(f"research record {record_id!r} not found", kind="NotFound")


@research_app.command("show")
def research_show(
    ctx: typer.Context,
    record_id: Annotated[str, typer.Argument(help="Research store record id.")],
    md: Annotated[bool, typer.Option("--md", help="Render markdown artifact body.")] = False,
) -> None:
    """Show one research store record."""
    from pydantic import ValidationError

    from eawf.kernel.store.kinds.research import ResearchPayload
    from eawf.render.research import render_research_markdown

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        envelope = _load_research_envelope(state_path, record_id)
        payload = ResearchPayload.model_validate(envelope.payload)
    except (errors.CliError, ValidationError) as exc:
        errors.emit_error(
            exc if isinstance(exc, errors.CliError) else errors.ValidationError(str(exc)),
            flags=flags,
        )
        return
    if md:
        if flags.json_output:
            errors.emit_error(
                errors.UserError("--md and --json are contradictory", kind="InvalidInput"),
                flags=flags,
            )
            return
        typer.echo(render_research_markdown(envelope, payload), nl=False)
        return
    body = {
        "id": envelope.id,
        "scope_id": envelope.scope_id,
        "topic": payload.topic,
        "findings": payload.findings,
        "references": [citation.model_dump(mode="json") for citation in payload.references],
    }
    emit_json_or_text(body, json.dumps(body, indent=2), flags=flags)
