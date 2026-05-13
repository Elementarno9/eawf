"""Research store read commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import orjson
import typer
from pydantic import ValidationError

from eawf.cli import errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.render.research import render_research_markdown
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope
from eawf.store.kinds.research import ResearchPayload
from eawf.store.paths import store_path

research_app = typer.Typer(
    name="research",
    help="Show and promote research briefs.",
    no_args_is_help=True,
)


def _load_research_envelope(state_path: Path, record_id: str) -> Envelope:
    path = store_path(state_path, StoreKind.RESEARCH)
    if not path.exists():
        raise errors.NotFound("research store is empty")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate(orjson.loads(line))
        if envelope.id == record_id:
            return envelope
    raise errors.NotFound(f"research record {record_id!r} not found")


@research_app.command("show")
def research_show(
    ctx: typer.Context,
    record_id: Annotated[str, typer.Argument(help="Research store record id.")],
    md: Annotated[bool, typer.Option("--md", help="Render markdown artifact body.")] = False,
) -> None:
    """Show one research store record."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        envelope = _load_research_envelope(state_path, record_id)
        payload = ResearchPayload.model_validate(envelope.payload)
    except (errors.CliError, ValidationError) as exc:
        errors.emit_error(
            exc if isinstance(exc, errors.CliError) else errors.ValidationFailed(str(exc)),
            flags=flags,
        )
        return
    if md:
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
