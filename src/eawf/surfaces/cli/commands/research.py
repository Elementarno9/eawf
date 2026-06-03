"""Research store read commands + the campaign-staging surface."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.commands.draft import install_promote_command
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.spec.research_campaign import ResearchProfileBlock
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload

logger = logging.getLogger(__name__)

research_app = typer.Typer(
    name="research",
    help="Show and promote research briefs.",
    no_args_is_help=True,
)

campaign_app = typer.Typer(
    name="campaign",
    help="Stage and persist multi-domain research campaigns.",
    no_args_is_help=True,
)
research_app.add_typer(campaign_app, name="campaign")

install_promote_command(research_app, "research")


def resolve_research_block(flags: GlobalFlags) -> ResearchProfileBlock | None:
    """Compose the active scope's enabled profiles and return the merged ``research:`` block.

    Mirrors the profile-composition entrypoint the PR-body command uses:
    merge the layered config for the active workspace/repo, compose every
    enabled profile, and read the last-non-``None``-wins ``research:`` block
    off the composed view. Returns ``None`` when no enabled profile declares
    a ``research:`` block.

    Args:
        flags: Resolved global flags carrying the optional workspace anchor.

    Returns:
        The merged :class:`~eawf.kernel.spec.research_campaign.ResearchProfileBlock`,
        or ``None`` when no enabled profile contributes one.
    """
    from eawf.kernel.config.layered import merge_config
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile

    merged, _sources = merge_config(workspace=flags.workspace, repo=Path.cwd())
    enabled = [str(pid) for pid in (merged.get("profiles", {}).get("enabled") or [])]
    composed = compose([load_profile(pid, workspace=flags.workspace) for pid in enabled])
    return composed.research


@campaign_app.command("new")
def campaign_new(
    ctx: typer.Context,
    topic: Annotated[str, typer.Argument(help="The campaign topic to fan out across domains.")],
) -> None:
    """Stage a research campaign for the active scope and persist it.

    Resolves the active scope's merged ``research:`` block, stages the
    campaign plan-only via
    :func:`~eawf.kernel.spec.research_campaign.stage_campaign`, and persists
    the resulting :class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`
    through the daemon ``research.create_campaign`` RPC -- falling back to a
    direct store append when the daemon is unavailable. The persisted row
    surfaces in the TUI Research board's topic tree.
    """
    from pydantic import ValidationError

    from eawf.kernel.spec.research_campaign import stage_campaign
    from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        block = resolve_research_block(flags)
        if block is None:
            raise errors.UserError(
                "no research: block configured for this scope", kind="InvalidInput"
            )
        campaign = stage_campaign(topic, block)
        campaign_id = f"campaign-{uuid.uuid4().hex}"
        payload = ResearchCampaignPayload(campaign_id=campaign_id, config=block, campaign=campaign)
    except ValueError as exc:
        # ``stage_campaign`` rejects an empty topic; the payload validator
        # rejects an over-bound dispatch count. Both surface as InvalidInput.
        errors.emit_error(errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    except (errors.CliError, ValidationError) as exc:
        errors.emit_error(
            exc if isinstance(exc, errors.CliError) else errors.ValidationError(str(exc)),
            flags=flags,
        )
        return

    appended_id = _persist_campaign_via_daemon_or_fallback(state_path, payload)
    body = {
        "campaign_id": campaign_id,
        "id": appended_id,
        "topic": campaign.topic,
        "domain_count": campaign.domain_count,
    }
    text = (
        f"staged campaign {campaign_id} "
        f"(topic={campaign.topic!r}, {campaign.domain_count} domain(s))"
    )
    emit_json_or_text(body, text, flags=flags)


def _persist_campaign_via_daemon_or_fallback(
    state_path: Path, payload: ResearchCampaignPayload
) -> str:
    """Persist *payload* through the daemon RPC, falling back to a direct append.

    Tries the daemon ``research.create_campaign`` RPC (the canonical writer per
    AGENTS rule 4). On ANY daemon failure -- a connection error or a
    :class:`~eawf.surfaces.cli._daemon_client.DaemonRpcError` (e.g. a daemon
    predating this method) -- falls back to the shared
    :func:`~eawf.runtime.daemon.methods.research.persist_campaign` helper so the
    command works offline / in CI / against a stale daemon (rule 4's
    daemon-proxy-then-portalocker-fallback).

    Args:
        state_path: Path to the scope's ``state.json``.
        payload: The validated :class:`ResearchCampaignPayload` to persist.

    Returns:
        The appended envelope id.
    """
    from eawf.runtime.daemon.methods.research import persist_campaign
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    params = payload.model_dump(mode="json")
    try:
        with DaemonClient() as client:
            result = client.call("research.create_campaign", params)
        return str(result["id"])
    except (DaemonRpcError, OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"_persist_campaign_via_daemon_or_fallback daemon_fallback cause={exc!r}")
        return persist_campaign(state_path, payload)


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
    from eawf.surfaces.render.research import render_research_markdown

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
