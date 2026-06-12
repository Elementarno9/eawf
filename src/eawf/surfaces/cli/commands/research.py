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

question_app = typer.Typer(
    name="question",
    help="Add and list research-campaign open questions.",
    no_args_is_help=True,
)
research_app.add_typer(question_app, name="question")

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


@question_app.command("add")
def question_add(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="The open-question text (1..72 chars).")],
    blocking: Annotated[
        bool,
        typer.Option("--blocking", help="Mark the question as gating further work (D-2)."),
    ] = False,
) -> None:
    """Add a research-campaign open question for the active scope.

    Proxies the daemon ``research.add_question`` RPC (the canonical writer for
    ``state.open_questions``), falling back to a direct ``state_transaction``
    write when the daemon is unavailable (CI / one-shot). The persisted row
    surfaces in the TUI Research board's tree + the ``eawf research question
    list`` verb.
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    result = _add_question_via_daemon_or_fallback(state_path, title=title, blocking=blocking)
    if result is None:
        errors.emit_error(
            errors.UserError("could not add question (over-cap title?)", kind="InvalidInput"),
            flags=flags,
        )
        return
    text = f"added question {result['question_id']} ({result['status']})"
    emit_json_or_text(result, text, flags=flags)


def _add_question_via_daemon_or_fallback(
    state_path: Path, *, title: str, blocking: bool
) -> dict[str, str] | None:
    """Add an open question through the daemon RPC, else a direct state write.

    Tries the daemon ``research.add_question`` RPC (the canonical writer per
    AGENTS rule 4). On ANY daemon failure -- a connection error or a typed
    rejection -- falls back to a direct ``state_transaction`` write so the verb
    works offline / in CI. Returns ``None`` when the write is rejected (an
    over-cap / empty title).

    Args:
        state_path: Path to the scope's ``state.json``.
        title: The question text.
        blocking: Whether the question gates further work.

    Returns:
        A result dict (``question_id`` / ``status`` / ``scope_id``), or ``None``
        on rejection.
    """
    import uuid

    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    params = {"title": title, "blocking": blocking, "repo_root": str(state_path.parent.parent)}
    try:
        with DaemonClient() as client:
            result = client.call("research.add_question", params)
        return {
            "question_id": str(result["question_id"]),
            "status": str(result["status"]),
            "scope_id": str(result["scope_id"]),
        }
    except DaemonRpcError as exc:
        logger.debug(f"_add_question daemon_rejected message={exc.message!r}")
        return None
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"_add_question daemon_fallback cause={exc!r}")

    # Offline fallback: write the row directly under portalock.
    from eawf.kernel.state.enums import OpenQuestionStatus
    from eawf.kernel.state.models import OpenQuestion
    from eawf.surfaces.cli._mutation import state_transaction

    if len(title) < 1 or len(title) > 72:
        return None
    question_id = f"OQ-{uuid.uuid4().hex[:8]}"
    status = OpenQuestionStatus.BLOCKED if blocking else OpenQuestionStatus.OPEN
    with state_transaction(state_path) as state:
        from datetime import UTC, datetime

        scope_id = state.project.code if state.project is not None else "research"
        questions = dict(state.open_questions or {})
        questions[question_id] = OpenQuestion(
            id=question_id,
            scope_id=scope_id,
            title=title,
            status=status,
            blocking=blocking,
            created_at=datetime.now(UTC),
        )
        state.open_questions = questions
    return {"question_id": question_id, "status": status.value, "scope_id": scope_id}


@question_app.command("list")
def question_list(ctx: typer.Context) -> None:
    """List the research-campaign open questions for the active scope.

    Reads ``state.open_questions`` off the resolved state (a read-only query --
    no daemon round-trip) and renders the still-open / blocked / answered /
    dropped rows. Exits 0 with an empty list when the scope has no question.
    """
    from pydantic import ValidationError

    from eawf.kernel.state.models import State

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
    except (errors.CliError, ValidationError, FileNotFoundError) as exc:
        errors.emit_error(
            exc if isinstance(exc, errors.CliError) else errors.ValidationError(str(exc)),
            flags=flags,
        )
        return
    rows = [
        {
            "id": question.id,
            "title": question.title,
            "status": question.status.value,
            "blocking": question.blocking,
        }
        for question in (state.open_questions or {}).values()
    ]
    rows.sort(key=lambda r: str(r["id"]))
    if rows:
        text = "\n".join(
            f"{r['id']} [{r['status']}{'/blocking' if r['blocking'] else ''}] {r['title']}"
            for r in rows
        )
    else:
        text = "no open questions"
    emit_json_or_text({"questions": rows}, text, flags=flags)


def _read_active_campaigns(state_path: Path) -> list[ResearchCampaignPayload]:
    """Return the scope's ACTIVE staged campaigns (latest-wins per id)."""
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
    from eawf.kernel.store.paths import store_path

    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return []
    latest: dict[str, ResearchCampaignPayload] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            envelope = Envelope.model_validate_json(line)
            payload = ResearchCampaignPayload.model_validate(envelope.payload)
            latest[payload.campaign_id] = payload
    return [p for p in latest.values() if p.status.value == "active"]


def _read_round_tallies(state_path: Path) -> tuple[int, int, bool]:
    """Return ``(rounds_run, checkpoints, saturated)`` off the round store."""
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.paths import store_path

    path = store_path(state_path, StoreKind.RESEARCH_ROUND)
    if not path.exists():
        return 0, 0, False
    rounds_run = 0
    checkpoints = 0
    saturated = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        rounds_run += 1
        if envelope.payload.get("checkpoint", False):
            checkpoints += 1
        if envelope.payload.get("saturated", False):
            saturated = True
    return rounds_run, checkpoints, saturated


def _read_question_counts(state_path: Path) -> tuple[int, int]:
    """Return ``(open_count, blocking_count)`` off the scope's question ledger."""
    from pydantic import ValidationError

    from eawf.kernel.state.enums import OpenQuestionStatus
    from eawf.kernel.state.models import State

    try:
        state = State.model_validate(orjson.loads(state_path.read_bytes()))
        questions = list((state.open_questions or {}).values())
    except ValidationError, FileNotFoundError:
        return 0, 0
    open_count = sum(
        1 for q in questions if q.status in (OpenQuestionStatus.OPEN, OpenQuestionStatus.BLOCKED)
    )
    blocking = sum(1 for q in questions if q.blocking)
    return open_count, blocking


@research_app.command("status")
def research_status(ctx: typer.Context) -> None:
    """Render the active scope's research campaign + round + checkpoint state.

    Folds the staged campaigns, the executed rounds, and the open-question
    ledger into a single campaign-progress summary (the
    :class:`~eawf.kernel.spec.operator_input.CampaignProgressState` answer to
    "can the campaign proceed") plus the per-campaign round + checkpoint tallies.
    Exits 0 with an honest "no campaign" line when the scope has staged none.
    """
    from eawf.kernel.spec.operator_input import (
        CampaignProgressState,
        DomainProgress,
        DomainProgressStatus,
    )

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return

    active = _read_active_campaigns(state_path)
    if not active:
        emit_json_or_text({"campaign": None}, "no research campaign staged", flags=flags)
        return
    rounds_run, checkpoints, saturated = _read_round_tallies(state_path)
    open_count, blocking = _read_question_counts(state_path)
    domain_status = DomainProgressStatus.SATURATED if saturated else DomainProgressStatus.READY
    domains = tuple(
        DomainProgress(domain=dispatch.domain, status=domain_status)
        for payload in active
        for dispatch in payload.campaign.dispatches
    )
    progress = CampaignProgressState.project(
        round_index=rounds_run,
        domains=domains,
        blocking_count=blocking,
    )
    body = {
        "campaign": {
            "campaigns": len(active),
            "kind": progress.kind.value,
            "rounds_run": rounds_run,
            "checkpoints": checkpoints,
            "open_questions": open_count,
            "saturated": saturated,
        }
    }
    text = (
        f"campaign: {progress.kind.value} "
        f"(campaigns={len(active)}, rounds={rounds_run}, checkpoints={checkpoints}, "
        f"open_questions={open_count})"
    )
    emit_json_or_text(body, text, flags=flags)
