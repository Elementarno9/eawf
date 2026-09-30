"""Research store read commands + the campaign and question entity groups.

``research`` keeps the store reads; ``campaign`` and ``question`` are entity
groups of their own, mounted at the CLI root by the command registry. The
``question`` group also files operator decisions, the pending actions an
agent raises when it reaches a choice it may not take.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import orjson
import typer

from eawf.kernel.state.enums import OpenQuestionDropReason, StoreKind
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.commands.draft import install_promote_command
from eawf.surfaces.cli.commands.question_decision import question_answer, question_open_decision
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
    from eawf.runtime.daemon.methods.research import AddQuestionParams

logger = logging.getLogger(__name__)

research_app = typer.Typer(
    name="research",
    help="Show and promote research briefs.",
    no_args_is_help=True,
)

campaign_app = typer.Typer(
    name="campaign",
    help="Plan, drive and cancel research Campaigns.",
    no_args_is_help=True,
)

question_app = typer.Typer(
    name="question",
    help="Add and list open questions, and file operator decisions.",
    no_args_is_help=True,
)

# an epoch-2 verb of this group, kept in a module of its own so it is not an epoch-1 one
question_app.command("open-decision")(question_open_decision)
question_app.command("answer")(question_answer)

install_promote_command(research_app, "research")


#: The daemon verbs the campaign group forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
CAMPAIGN_START: Final = "runtime.campaign.start"
CAMPAIGN_RUN: Final = "runtime.campaign.run"
CAMPAIGN_CLOSE: Final = "runtime.campaign.close"
CAMPAIGN_VIEW: Final = "projection.campaign.view"

_CampaignKey = Annotated[str, typer.Argument(help="The Campaign's CAM-#### key.")]
_Actor = Annotated[str, typer.Option("--actor", help="Principal key the change is made as.")]
_Agents = Annotated[
    int | None,
    typer.Option(
        "--agents",
        min=1,
        max=12,
        help="Ready steps one round dispatches; the research.agent_count layer otherwise.",
    ),
]
_Runtime = Annotated[
    str, typer.Option("--runtime", help="Runtime whose headless session works each round.")
]


def _campaign_call(
    ctx: typer.Context, method: str, params: dict[str, Any], verb_text: str
) -> dict[str, Any] | None:
    """Send one campaign verb and return its answer, or emit the error and return ``None``."""
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer

    flags: GlobalFlags = ctx.obj
    try:
        return _native_answer(
            method,
            params,
            flags=flags,
            verb_text=verb_text,
            read=method == CAMPAIGN_VIEW,
        )
    except DaemonRpcError as exc:
        errors.emit_error(errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
    return None


@campaign_app.command("new")
def campaign_new(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="What the Campaign researches, in one line.")],
    actor: _Actor,
    track: Annotated[str, typer.Option("--track", help="URN of the Track that owns it.")],
    question: Annotated[
        list[str],
        typer.Option("--question", help="A question to work; repeat it, the Campaign's own first."),
    ],
    depth: Annotated[
        str | None,
        typer.Option(
            "--depth",
            help="shallow|medium|deep|exhaustive; the research.default_depth layer otherwise.",
        ),
    ] = None,
    agents: _Agents = None,
    budget_rounds: Annotated[
        int | None,
        typer.Option("--budget-rounds", min=1, help="Hard limit on the rounds it runs."),
    ] = None,
    budget_tokens: Annotated[
        int | None,
        typer.Option("--budget-tokens", min=1, help="Hard limit on the tokens its rounds spend."),
    ] = None,
    runtime: _Runtime = "claude-code",
    run: Annotated[
        bool, typer.Option("--run/--no-run", help="Start driving it once it is approved.")
    ] = False,
) -> None:
    """Plan a research Campaign from its brief and approve the plan.

    The daemon files each question, plans one step per question and method
    the depth names plus a closing synthesis, and approves the plan under the
    Track. With ``--run`` it also starts driving it; ``campaign run`` does so
    later. A budget left unset bounds the Campaign by one round per step.
    """
    axes = [
        {"axis_kind": kind, "limit": limit, "unit": kind}
        for kind, limit in (("rounds", budget_rounds), ("tokens", budget_tokens))
        if limit is not None
    ]
    params: dict[str, Any] = {
        "actor": actor,
        "track_ref": track,
        "title": title,
        "questions": question,
        "runtime": runtime,
        "drive": run,
    }
    params |= {
        key: value
        for key, value in (
            ("depth", depth),
            ("agents", agents),
            ("budget", {"axes": axes} if axes else None),
        )
        if value is not None
    }
    answer = _campaign_call(ctx, CAMPAIGN_START, params, "campaign new")
    if answer is None:
        return
    record = answer["record"]
    text = f"approved campaign {record['key']} ({len(record['plan_steps'])} steps)" + (
        "; driving" if answer["driving"] else ""
    )
    emit_json_or_text(answer, text, flags=ctx.obj)


@campaign_app.command("run")
def campaign_run(
    ctx: typer.Context,
    campaign_key: _CampaignKey,
    actor: _Actor,
    agents: _Agents = None,
    runtime: _Runtime = "claude-code",
) -> None:
    """Drive an approved Campaign round by round in the daemon.

    Each ready step runs on a Run of its own; each round checkpoints its
    report as an artifact revision, charges the budget and finishes its step.
    The drive runs behind the answer: follow it in the console's Campaign
    screen. A hard budget axis at its limit stops it and says why.
    """
    params: dict[str, Any] = {"actor": actor, "campaign_key": campaign_key, "runtime": runtime}
    if agents is not None:
        params["agents"] = agents
    answer = _campaign_call(ctx, CAMPAIGN_RUN, params, "campaign run")
    if answer is None:
        return
    text = (
        f"driving campaign {campaign_key}"
        if answer["driving"]
        else f"campaign {campaign_key} is already being driven"
    )
    emit_json_or_text(answer, text, flags=ctx.obj)


@campaign_app.command("cancel")
def campaign_cancel(
    ctx: typer.Context,
    campaign_key: _CampaignKey,
    actor: _Actor,
    reason: Annotated[str, typer.Option("--reason", help="Why it is abandoned, in one line.")],
) -> None:
    """Cancel an active Campaign, recording why; its steps and artifacts stay."""
    view = _campaign_call(ctx, CAMPAIGN_VIEW, {"campaign_key": campaign_key}, "campaign cancel")
    if view is None:
        return
    params = {
        "actor": actor,
        "urn": view["campaign_ref"],
        "expected_revision": view["revision"],
        "to_status": "cancelled",
        "reason": reason,
    }
    answer = _campaign_call(ctx, CAMPAIGN_CLOSE, params, "campaign cancel")
    if answer is None:
        return
    emit_json_or_text(answer, f"cancelled campaign {campaign_key}", flags=ctx.obj)


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
    title: Annotated[
        str | None, typer.Argument(help="The open-question text (1..72 chars).")
    ] = None,
    blocking: Annotated[
        bool,
        typer.Option("--blocking", help="Mark the question as gating further work (D-2)."),
    ] = False,
    from_spec: Annotated[
        Path | None,
        typer.Option(
            "--from-spec",
            help="JSON file (or - for stdin) carrying the whole question, in place of the flags.",
        ),
    ] = None,
) -> None:
    """Add a research-campaign open question for the active scope.

    Proxies the daemon ``research.add_question`` RPC (the canonical writer for
    ``state.open_questions``), falling back to a direct ``state_transaction``
    write when the daemon is unavailable (CI / one-shot). The persisted row
    surfaces in the TUI Research board's tree + the ``eawf question list``
    verb. The title and ``--blocking``, or the ``--from-spec`` document, parse
    through the RPC's own closed params model before anything is sent.
    """
    from eawf.runtime.daemon.methods.research import AddQuestionParams
    from eawf.surfaces.cli.verb_contract import request_document

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        request = request_document(
            AddQuestionParams, from_spec, {"title": title, "blocking": blocking or None}
        )
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    result = _add_question_via_daemon_or_fallback(state_path, request)
    if result is None:
        errors.emit_error(
            errors.UserError("the daemon refused the question", kind="InvalidInput"),
            flags=flags,
        )
        return
    text = f"added question {result['question_id']} ({result['status']})"
    emit_json_or_text(result, text, flags=flags)


def _add_question_via_daemon_or_fallback(
    state_path: Path, request: AddQuestionParams
) -> dict[str, str] | None:
    """Add an open question through the daemon RPC, else a direct state write.

    Tries the daemon ``research.add_question`` RPC (the canonical writer per
    AGENTS rule 4). On a connection failure falls back to a direct
    ``state_transaction`` write so the verb works offline / in CI; a typed
    rejection from the daemon returns ``None``.

    Args:
        state_path: Path to the scope's ``state.json``.
        request: The validated question.

    Returns:
        A result dict (``question_id`` / ``status`` / ``scope_id``), or ``None``
        on rejection.
    """

    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    params = {
        **request.model_dump(mode="json", exclude_none=True),
        "repo_root": str(state_path.parent.parent),
    }
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

    question_id = request.question_id or f"OQ-{uuid.uuid4().hex[:8]}"
    status = OpenQuestionStatus.BLOCKED if request.blocking else OpenQuestionStatus.OPEN
    with state_transaction(state_path) as state:
        from datetime import UTC, datetime

        project_code = state.project.code if state.project is not None else "research"
        scope_id = request.scope_id or project_code
        questions = dict(state.open_questions or {})
        questions[question_id] = OpenQuestion(
            id=question_id,
            scope_id=scope_id,
            title=request.title,
            description=request.description,
            status=status,
            blocking=request.blocking,
            urgency=request.urgency,
            created_at=datetime.now(UTC),
        )
        state.open_questions = questions
    return {"question_id": question_id, "status": status.value, "scope_id": scope_id}


@question_app.command("resolve")
def question_resolve(
    ctx: typer.Context,
    question_id: Annotated[str, typer.Argument(help="The open-question id to resolve.")],
    drop: Annotated[
        bool,
        typer.Option("--drop", help="Mark the question DROPPED (out of scope), not ANSWERED."),
    ] = False,
    reason: Annotated[
        OpenQuestionDropReason | None,
        typer.Option("--reason", help="Why the question is dropped; requires --drop."),
    ] = None,
    superseded_by: Annotated[
        str | None,
        typer.Option("--superseded-by", help="The successor question id; requires --drop."),
    ] = None,
) -> None:
    """Resolve a blocking / open research-campaign question for the active scope.

    Proxies the daemon ``research.resolve_question`` RPC (the canonical writer
    for ``state.open_questions``), falling back to a direct ``state_transaction``
    write when the daemon is unavailable (CI / one-shot). Resolving flips the
    question to a terminal status (ANSWERED, or DROPPED with ``--drop``) and
    clears its ``blocking`` bit, so a campaign halted on the balanced-autonomy
    interrupt (a blocking question, D-2) resumes. A drop records ``--reason``
    and ``--superseded-by`` when given.
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    params: dict[str, object] = {"question_id": question_id, "drop": drop}
    if reason is not None:
        params["drop_reason"] = reason.value
    if superseded_by is not None:
        params["superseded_by_question_ref"] = superseded_by
    result = _resolve_question_via_daemon_or_fallback(state_path, params=params)
    if result is None:
        errors.emit_error(
            errors.UserError(
                f"could not resolve question {question_id!r} (unknown id or successor?)",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    text = f"resolved question {result['question_id']} ({result['status']})"
    emit_json_or_text(result, text, flags=flags)


def _resolve_question_via_daemon_or_fallback(
    state_path: Path, *, params: dict[str, object]
) -> dict[str, str] | None:
    """Resolve an open question through the daemon RPC, else a direct state write.

    Tries the daemon ``research.resolve_question`` RPC (the canonical writer per
    AGENTS rule 4). On ANY daemon failure -- a connection error or a typed
    rejection -- falls back to a direct ``state_transaction`` write so the verb
    works offline / in CI; the fallback applies the daemon's own resolve so
    both paths record the same disposition. Returns ``None`` when the resolve
    is refused (an unknown id or successor, or a drop field without a drop).

    Args:
        state_path: Path to the scope's ``state.json``.
        params: The ``research.resolve_question`` params without ``repo_root``.

    Returns:
        A result dict (``question_id`` / ``status`` / ``scope_id``), or ``None``
        when the resolve is refused.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient() as client:
            result = client.call(
                "research.resolve_question",
                {**params, "repo_root": str(state_path.parent.parent)},
            )
        return {
            "question_id": str(result["question_id"]),
            "status": str(result["status"]),
            "scope_id": str(result["scope_id"]),
        }
    except DaemonRpcError as exc:
        logger.debug(f"_resolve_question daemon_rejected message={exc.message!r}")
        return None
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"_resolve_question daemon_fallback cause={exc!r}")

    # Offline fallback: write the row directly under portalock.
    from pydantic import ValidationError

    from eawf.runtime.daemon.methods.research import (
        ResolveQuestionParams,
        _apply_resolve_question,
    )
    from eawf.surfaces.cli._mutation import state_transaction

    try:
        args = ResolveQuestionParams.model_validate(params)
        with state_transaction(state_path) as state:
            result = _apply_resolve_question(state, args)
    except (ValidationError, ValueError) as exc:
        logger.debug(f"_resolve_question fallback_refused cause={exc!r}")
        return None
    return {key: str(result[key]) for key in ("question_id", "status", "scope_id")}


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


def _read_campaigns(state_path: Path) -> list[ResearchCampaignPayload]:
    """Return the scope's staged campaigns in any status (latest-wins per id)."""
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
    return list(latest.values())


def _read_active_campaigns(state_path: Path) -> list[ResearchCampaignPayload]:
    """Return the scope's ACTIVE staged campaigns (latest-wins per id)."""
    return [p for p in _read_campaigns(state_path) if p.status.value == "active"]


def _read_campaign_cost(state_path: Path, campaigns: list[ResearchCampaignPayload]) -> Decimal:
    """Return the researcher spend booked against *campaigns*.

    A campaign books its researcher spend to its own cost centre rather than to
    an execution wave, so the total is summed off the campaign-scoped
    ``dispatch_cost`` events rather than off any wave's counters. The spend of a
    campaign that has CONVERGED or been CANCELLED counts the same as a running
    one's: what a campaign cost is asked AFTER it finishes, not only while it
    runs, so a terminal campaign must not take its bill with it.

    Args:
        state_path: Path to the scope's ``state.json``.
        campaigns: The staged campaigns whose spend to total, in any status.

    Returns:
        The summed researcher spend in USD across *campaigns*.
    """
    from eawf.runtime.daemon.methods.research import read_campaign_costs

    totals = read_campaign_costs(state_path, [payload.campaign_id for payload in campaigns])
    return sum(totals.values(), Decimal("0"))


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

    A scope whose campaigns have all reached a terminal state (CONVERGED or
    CANCELLED) still reports what they cost: the operator asks what a campaign
    spent after it finishes, not only while it runs, so a terminal campaign does
    not take its bill with it.
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

    campaigns = _read_campaigns(state_path)
    if not campaigns:
        emit_json_or_text({"campaign": None}, "no research campaign staged", flags=flags)
        return
    cost_usd = _read_campaign_cost(state_path, campaigns)
    active = [payload for payload in campaigns if payload.status.value == "active"]
    if not active:
        terminal = len(campaigns)
        emit_json_or_text(
            {"campaign": {"campaigns": 0, "terminal": terminal, "cost_usd": float(cost_usd)}},
            f"campaign: none active ({terminal} terminal, cost=${cost_usd:.2f})",
            flags=flags,
        )
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
            "cost_usd": float(cost_usd),
        }
    }
    text = (
        f"campaign: {progress.kind.value} "
        f"(campaigns={len(active)}, rounds={rounds_run}, checkpoints={checkpoints}, "
        f"open_questions={open_count}, cost=${cost_usd:.2f})"
    )
    emit_json_or_text(body, text, flags=flags)
