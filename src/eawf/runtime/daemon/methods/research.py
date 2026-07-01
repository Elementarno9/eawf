"""``research.*`` JSON-RPC methods: typed append to ``research_campaign.jsonl``.

The :func:`create_campaign` method is the daemon-canonical writer for
``<state_dir>/store/research_campaign.jsonl``. The ``eawf research campaign
new`` CLI surface proxies a staged campaign through this RPC so the
single-writer invariant in AGENTS rule 4 holds; it falls back to the shared
:func:`persist_campaign` helper directly only when the daemon is unavailable
(CI / one-shot / a daemon predating this method).

The append is **non-state**: no
:class:`~eawf.kernel.state.mutations.MutationKind` is allocated and the
daemon's WAL recovery path treats campaign rows as derivable replay no-ops,
same as event / audit / evidence appends. Downstream consumers (the Research
board topic tree) re-validate the row by reading the envelope back and running
``ResearchCampaignPayload.model_validate(envelope.payload)``.
"""
# noqa: EAWF010 cohesive research-campaign command surface (stage / run / round
# persist / operator channel); the read-only payload + helper split is deferred
# to a follow-up once the control plane stops accreting per binding wave.

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.layered import merge_config, resolve_runtime_tier_models
from eawf.kernel.spec.campaign_driver import (
    DispatchSpawner,
    RoundFindings,
    RoundSaturationReducer,
    build_round_runner,
    drive_campaign,
)
from eawf.kernel.spec.live_rounds import CockpitLevel
from eawf.kernel.spec.operator_input import (
    AddQuestionPayload,
    ChannelFold,
    OperatorInput,
    OperatorInputChannel,
    OperatorInputKind,
    OverridePayload,
    SteerAction,
    SteerPayload,
)
from eawf.kernel.spec.pruning import prune_round_carryover
from eawf.kernel.spec.research_campaign import (
    ResearchProfileBlock,
    StagedCampaign,
    stage_campaign,
)
from eawf.kernel.spec.round_loop import DEFAULT_ROUND_BUDGET, CheckpointPolicy, CheckpointTier
from eawf.kernel.state.enums import (
    AgentSessionRole,
    CampaignStatus,
    ClaimStatus,
    EffortBucket,
    OpenQuestionStatus,
    StoreKind,
    Urgency,
)
from eawf.kernel.state.io import state_version
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.kinds.events.base import RuntimeTriple
from eawf.kernel.store.kinds.research_campaign import (
    CampaignTombstone,
    ResearchCampaignPayload,
)
from eawf.kernel.store.kinds.research_round import ResearchRoundPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.budget.policy import DEFAULT_ENFORCE, EnforceMode
from eawf.runtime.daemon.dispatch_runner import (
    _CHUNK_BATCH_LINES,
    DispatchTokens,
    emit_agent_output_chunk,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.metering import price_spawn_result
from eawf.runtime.runtimes.selector import select_adapter
from eawf.runtime.session.store import SessionConflict, append_event, start_session
from eawf.workflow.dispatch.llm_assist import assist_with_schema
from eawf.workflow.dispatch.routing import model_for_runtime
from eawf.workflow.evidence._io import load_state

if TYPE_CHECKING:
    from eawf.kernel.spec.research_campaign import StagedDispatch
    from eawf.kernel.spec.round_loop import RoundOutcome
    from eawf.kernel.state.models import Claim, State

logger = logging.getLogger(__name__)

#: Type of the per-dispatch agent-end producer the live dispatch spawner
#: wraps. Production binds a closure that drives the daemon ``agent.dispatch``
#: spawn=True path for a researcher dispatch and returns the spawned agent's
#: decoded ``agent_end`` body; a test binds a stub returning a fixture body.
#: Keeping the producer injected means the binding (StagedDispatch ->
#: agent.dispatch -> agent_end parse) is exercised under a stubbed spawner +
#: fixture bodies without spawning a real subprocess.
AgentEndProducer = Callable[["StagedDispatch"], Mapping[str, object]]

_RUNTIME_ALIASES: dict[str, str] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
}

_RUNTIME_TRIPLES: dict[str, RuntimeTriple] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def build_live_dispatch_spawner(produce_agent_end: AgentEndProducer) -> DispatchSpawner:
    """Build the production per-dispatch spawn seam for the round runner.

    The seam :func:`~eawf.kernel.spec.campaign_driver.build_round_runner` drives
    once per :class:`~eawf.kernel.spec.research_campaign.StagedDispatch`: it
    converts the staged read-only researcher dispatch into a real spawned
    researcher session and returns the spawned agent's decoded ``agent_end``
    body, which the round runner parses into typed findings rows
    (:func:`~eawf.kernel.spec.campaign_driver.parse_researcher_findings`).

    The actual spawn is injected as *produce_agent_end* so this daemon-side
    binding is exercised with a stubbed spawner + fixture ``agent_end`` bodies
    in tests rather than spawning a real subprocess. Production wires
    *produce_agent_end* to a closure over the ``agent.dispatch`` spawn=True path
    (which registers an executor / researcher session behind the safety floor
    and binds the spawned agent's own output to a typed report body).

    Args:
        produce_agent_end: The per-dispatch agent-end producer (production:
            the live ``agent.dispatch`` spawn; a test: a fixture stub).

    Returns:
        A :class:`~eawf.kernel.spec.campaign_driver.DispatchSpawner` the round
        runner drives once per staged dispatch.
    """

    def _spawn(dispatch: StagedDispatch) -> Mapping[str, object]:
        logger.info(
            f"build_live_dispatch_spawner domain={dispatch.domain!r} role={dispatch.agent_role!r}"
        )
        return produce_agent_end(dispatch)

    return _spawn


def build_bound_round_runner(
    campaign: StagedCampaign,
    produce_agent_end: AgentEndProducer,
    saturation: RoundSaturationReducer,
) -> tuple[Callable[[int], RoundOutcome], list[Any]]:
    """Bind a campaign's round runner over the live ``agent.dispatch`` spawn.

    Composes :func:`build_live_dispatch_spawner` with
    :func:`~eawf.kernel.spec.campaign_driver.build_round_runner` so the
    ``drive_campaign`` dispatcher receives a runner whose every round spawns a
    real researcher session per staged dispatch and parses each ``agent_end``
    body into findings rows. The spawn itself is injected (*produce_agent_end*)
    so the binding is unit-testable under a stub.

    Args:
        campaign: The staged campaign whose dispatches the runner spawns.
        produce_agent_end: The per-dispatch agent-end producer (the live
            spawn in production; a fixture stub in tests).
        saturation: The per-round saturation reducer the runner consults.

    Returns:
        The ``(round_runner, rounds)`` pair the campaign driver consumes.
    """
    spawner = build_live_dispatch_spawner(produce_agent_end)
    return build_round_runner(campaign, spawner, saturation)


class CreateCampaignParams(BaseModel):
    """Params for :func:`create_campaign`.

    Attributes:
        campaign_id: Stable caller-allocated id for the staged campaign.
        config: The typed ``research:`` block the campaign was staged from.
        campaign: The plan-only :class:`StagedCampaign` the Level-1 runner
            emitted. Validated into a :class:`ResearchCampaignPayload` before
            any side effect.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    config: ResearchProfileBlock
    campaign: StagedCampaign


class CreateCampaignResult(BaseModel):
    """Result of :func:`create_campaign`.

    Attributes:
        id: Envelope id of the campaign row just appended (mirrors
            :attr:`ResearchCampaignPayload.campaign_id`).
        appended_at: ISO-8601 timestamp the daemon wrote the row.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    appended_at: str


def persist_campaign(state_path: Path, payload: ResearchCampaignPayload) -> str:
    """Append *payload* as one row to ``research_campaign.jsonl`` and return its id.

    Wraps the typed :class:`ResearchCampaignPayload` in an :class:`Envelope`
    with ``kind=StoreKind.RESEARCH_CAMPAIGN`` and appends it via
    :func:`eawf.kernel.store.append.append_envelope` (per-file portalock +
    fsync). The on-disk row is the single source of truth; no projection runs
    because a campaign record is a non-state append.

    Shared by both the :func:`create_campaign` RPC handler and the CLI
    offline-fallback so the persistence logic has exactly one home (AGENTS
    DRY rule).

    Args:
        state_path: Path to the scope's ``state.json``; the campaign store
            resolves under its sibling ``store/`` directory.
        payload: The validated campaign payload to persist.

    Returns:
        The appended envelope id (equal to ``payload.campaign_id``).

    Raises:
        StateConflict: When the campaign-store append lock cannot be acquired
            within the canonical timeout (``kind="LockConflict"``).
    """
    campaign_path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id=None,
        created_at=datetime.now(UTC),
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(campaign_path, envelope)
    logger.info(
        f"persist_campaign id={payload.campaign_id!r} "
        f"topic={payload.campaign.topic!r} dispatches={len(payload.campaign.dispatches)}"
    )
    return payload.campaign_id


@register("research.create_campaign")
async def create_campaign(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Validate a staged campaign and append one row to ``research_campaign.jsonl``.

    The handler validates the input through :class:`CreateCampaignParams`,
    builds the typed :class:`ResearchCampaignPayload` (whose model validator
    rejects an over-bound dispatch count), and persists it via the shared
    :func:`persist_campaign` helper.

    Args:
        ctx: Server context -- must carry ``state_path`` so the daemon can
            resolve ``<state_dir>/store/research_campaign.jsonl``.
        params: JSON-RPC params per :class:`CreateCampaignParams`.

    Returns:
        Dict matching :class:`CreateCampaignResult`.

    Raises:
        ValueError: When *params* does not validate against
            :class:`CreateCampaignParams` or the payload exceeds the staged-
            dispatch bound. The server maps this to ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset (unit tests running the
            daemon without an on-disk store).
    """
    args = CreateCampaignParams.model_validate(params)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    payload = ResearchCampaignPayload(
        campaign_id=args.campaign_id,
        config=args.config,
        campaign=args.campaign,
    )
    appended_id = persist_campaign(Path(ctx.state_path), payload)
    appended_at = datetime.now(UTC).isoformat()
    return CreateCampaignResult(id=appended_id, appended_at=appended_at).model_dump(mode="json")


class StageCampaignParams(BaseModel):
    """Params for :func:`stage_campaign_method`.

    Attributes:
        topic: The campaign topic to fan out across the block's domains. Must
            be a non-empty, non-whitespace string -- an empty topic is rejected
            by :func:`~eawf.kernel.spec.research_campaign.stage_campaign`, which
            the server maps to ``-32602 invalid params``.
        config: The typed ``research:`` block the campaign is staged from; its
            per-domain map decides how many dispatches the staged plan carries.
        campaign_id: Optional caller-allocated stable id for the staged
            campaign. ``None`` allocates a fresh ``campaign-<hex>`` id so the
            common caller (the Research board) need not mint one.
    """

    model_config = ConfigDict(extra="forbid")
    topic: str
    config: ResearchProfileBlock
    campaign_id: str | None = Field(default=None, min_length=1)


class StageCampaignResult(BaseModel):
    """Result of :func:`stage_campaign_method`.

    Attributes:
        id: Envelope id of the campaign row just appended (equal to the
            campaign id, caller-supplied or freshly allocated).
        campaign_id: The id of the staged campaign (mirrors :attr:`id`).
        topic: The staged campaign's topic.
        domain_count: Number of staged dispatches (one per configured domain).
        appended_at: ISO-8601 timestamp the daemon wrote the row.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    campaign_id: str
    topic: str
    domain_count: int
    appended_at: str


@register("research.stage_campaign")
async def stage_campaign_method(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Stage a campaign from a topic + block and append one row to the store.

    Wraps the plan-only Level-1 runner
    :func:`~eawf.kernel.spec.research_campaign.stage_campaign`: it validates the
    input through :class:`StageCampaignParams`, stages the topic across the
    block's domains (sorted domain order, no spawn), wraps the staged plan in a
    typed :class:`ResearchCampaignPayload`, and persists it via the shared
    :func:`persist_campaign` writer. The staged ``campaign.dispatches`` carry
    one entry per configured domain in sorted domain-name order.

    Args:
        ctx: Server context -- must carry ``state_path`` so the daemon can
            resolve ``<state_dir>/store/research_campaign.jsonl``.
        params: JSON-RPC params per :class:`StageCampaignParams`.

    Returns:
        Dict matching :class:`StageCampaignResult`.

    Raises:
        ValueError: When *params* does not validate against
            :class:`StageCampaignParams`, the topic is empty or whitespace, or
            the staged plan exceeds the dispatch bound. The server maps this to
            ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset (unit tests running the
            daemon without an on-disk store).
    """
    args = StageCampaignParams.model_validate(params)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    campaign = stage_campaign(args.topic, args.config)
    campaign_id = (
        args.campaign_id if args.campaign_id is not None else f"campaign-{uuid.uuid4().hex}"
    )
    payload = ResearchCampaignPayload(
        campaign_id=campaign_id,
        config=args.config,
        campaign=campaign,
    )
    appended_id = persist_campaign(Path(ctx.state_path), payload)
    appended_at = datetime.now(UTC).isoformat()
    return StageCampaignResult(
        id=appended_id,
        campaign_id=campaign_id,
        topic=campaign.topic,
        domain_count=campaign.domain_count,
        appended_at=appended_at,
    ).model_dump(mode="json")


class CancelCampaignParams(BaseModel):
    """Params for :func:`cancel_campaign`.

    Attributes:
        campaign_id: Id of the campaign to cancel; must name an ACTIVE
            campaign already present in the store.
        reason: Optional short operator-supplied reason recorded on the
            campaign's tombstone; ``None`` records no reason.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=280)


class CancelCampaignResult(BaseModel):
    """Result of :func:`cancel_campaign`.

    Attributes:
        id: The cancelled campaign's id (mirrors the input ``campaign_id``).
        status: The campaign's new lifecycle status value (``"cancelled"``).
        cancelled_at: ISO-8601 timestamp the tombstone records.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    status: str
    cancelled_at: str


def read_latest_campaign(state_path: Path, campaign_id: str) -> ResearchCampaignPayload | None:
    """Return the most-recent persisted payload for *campaign_id*, or ``None``.

    Walks the append-only ``research_campaign.jsonl`` store under *state_path*
    in record order, validating each envelope + payload, and returns the LAST
    row matching *campaign_id* so a campaign that has been re-appended (e.g. a
    cancel that stamps a fresh tombstoned row) resolves to its current state.
    Returns ``None`` when the store is absent or carries no row for the id.

    Args:
        state_path: Path to the scope's ``state.json``; the campaign store
            resolves under its sibling ``store/`` directory.
        campaign_id: The campaign id to resolve.

    Returns:
        The latest matching :class:`ResearchCampaignPayload`, or ``None``.
    """
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return None
    latest: ResearchCampaignPayload | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchCampaignPayload.model_validate(envelope.payload)
        if payload.campaign_id == campaign_id:
            latest = payload
    return latest


@register("research.cancel_campaign")
async def cancel_campaign(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Tombstone an ACTIVE campaign by appending a cancelled copy of its row.

    The campaign store is append-only, so cancelling does not delete the
    original row: the handler reads the campaign's most-recent payload
    (:func:`read_latest_campaign`), requires it to be ACTIVE, and appends a
    fresh copy carrying ``status=CANCELLED`` + a :class:`CampaignTombstone`
    (cancel time + optional reason) via the shared :func:`persist_campaign`
    writer. Re-cancelling an already-cancelled campaign is rejected so the
    cancel is idempotent only by explicit operator intent.

    Args:
        ctx: Server context -- must carry ``state_path`` so the daemon can
            resolve ``<state_dir>/store/research_campaign.jsonl``.
        params: JSON-RPC params per :class:`CancelCampaignParams`.

    Returns:
        Dict matching :class:`CancelCampaignResult`.

    Raises:
        ValueError: When *params* does not validate, the campaign id names no
            stored campaign, or the campaign is not ACTIVE. The server maps
            this to ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = CancelCampaignParams.model_validate(params)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    current = read_latest_campaign(state_path, args.campaign_id)
    if current is None:
        raise ValueError(f"unknown campaign: {args.campaign_id!r}")
    if current.status is not CampaignStatus.ACTIVE:
        raise ValueError(f"campaign not active: {args.campaign_id!r} is {current.status.value!r}")
    cancelled_at = datetime.now(UTC)
    tombstoned = current.model_copy(
        update={
            "status": CampaignStatus.CANCELLED,
            "tombstone": CampaignTombstone(cancelled_at=cancelled_at, reason=args.reason),
        }
    )
    persist_campaign(state_path, tombstoned)
    logger.info(f"cancel_campaign id={args.campaign_id!r} reason={args.reason!r}")
    return CancelCampaignResult(
        id=args.campaign_id,
        status=CampaignStatus.CANCELLED.value,
        cancelled_at=cancelled_at.isoformat(),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------
# OpenQuestion writer -- the o-key channel + the campaign question ledger
# --------------------------------------------------------------------------


def _resolve_research_scope(state: State, scope_id: str | None) -> str:
    """Resolve the scope a research claim / question is bound to.

    An explicit *scope_id* wins; otherwise the active project's code anchors
    the row (the campaign is scoped to the project, like every other research
    entity). A scope-less state (no project) falls back to the literal
    ``"research"`` so the row still validates.

    Args:
        state: The loaded state the row is being added to.
        scope_id: An explicit caller-supplied scope id, or ``None``.

    Returns:
        The resolved non-empty scope id.
    """
    if scope_id:
        return scope_id
    if state.project is not None:
        return state.project.code
    return "research"


class AddQuestionParams(BaseModel):
    """Params for :func:`add_question`.

    The campaign control-plane ``add_question`` channel: the operator (via the
    TUI ``o`` key or the headless ``eawf research question add`` verb) injects a
    new :class:`~eawf.kernel.state.models.OpenQuestion` into the campaign ledger
    mid-run. The TUI sends only ``title``; the other fields default so the row
    lands as an ordinary advisory open question unless the operator escalates
    it.

    Attributes:
        title: The question text, an imperative noun-phrase bounded to 1..72
            chars to match the :class:`~eawf.kernel.state.models.OpenQuestion`
            model's title bound. An over-cap / empty title is rejected fail-fast
            at the params boundary (``-32602 invalid params``) before any side
            effect.
        description: Optional long-form framing for the question.
        blocking: Whether the question gates further campaign work (the
            balanced-autonomy interrupt). Defaults ``False`` (advisory).
        urgency: The shared :class:`~eawf.kernel.state.enums.Urgency` rung the
            question inherits. Defaults to ``NORMAL``.
        scope_id: Explicit scope the question binds to; ``None`` resolves to
            the active project's code.
        question_id: Optional caller-allocated id; ``None`` allocates a fresh
            ``OQ-<hex>`` id.
        repo_root: Optional per-request repo anchor for the worktree-aware
            state writer; ``None`` uses the daemon's boot-time state path.
    """

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=72)
    description: str | None = Field(default=None, max_length=500)
    blocking: bool = False
    urgency: Urgency = Urgency.NORMAL
    scope_id: str | None = None
    question_id: str | None = Field(default=None, min_length=1)
    repo_root: str | None = None


class AddQuestionResult(BaseModel):
    """Result of :func:`add_question`.

    Attributes:
        question_id: The id of the open question just written.
        status: The question's lifecycle status (``"open"`` /
            ``"blocked"`` when the operator marked it blocking).
        scope_id: The resolved scope the question was bound to.
    """

    model_config = ConfigDict(extra="forbid")
    question_id: str
    status: str
    scope_id: str


def _apply_add_question(
    state: State, args: AddQuestionParams, *, question_id: str
) -> dict[str, Any]:
    """Write one :class:`OpenQuestion` row onto ``state.open_questions``.

    A blocking question lands ``BLOCKED`` (the interrupt status); an advisory
    one lands ``OPEN``. The row is constructed through the typed model, so an
    over-cap / empty title raises :class:`pydantic.ValidationError` -- the
    canonical writer maps it to ``-32002 validation_failed``.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        args: Validated :class:`AddQuestionParams`.
        question_id: The resolved (caller or freshly allocated) question id.

    Returns:
        Result dict matching :class:`AddQuestionResult`.
    """
    from eawf.kernel.state.models import OpenQuestion

    status = OpenQuestionStatus.BLOCKED if args.blocking else OpenQuestionStatus.OPEN
    scope_id = _resolve_research_scope(state, args.scope_id)
    questions = dict(state.open_questions or {})
    questions[question_id] = OpenQuestion(
        id=question_id,
        scope_id=scope_id,
        title=args.title,
        description=args.description,
        status=status,
        blocking=args.blocking,
        urgency=args.urgency,
        created_at=datetime.now(UTC),
    )
    state.open_questions = questions
    logger.info(
        f"_apply_add_question question={question_id!r} scope={scope_id!r} "
        f"blocking={args.blocking} status={status.value}"
    )
    return AddQuestionResult(
        question_id=question_id,
        status=status.value,
        scope_id=scope_id,
    ).model_dump(mode="json")


@register("research.add_question")
async def add_question(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Write an :class:`OpenQuestion` row through the canonical state writer.

    The daemon-canonical mutator for ``state.open_questions`` (AGENTS rule 4);
    the TUI ``o`` key + the headless ``eawf research question add`` verb proxy
    here. The row lands through the same per-file portalock + WAL + event-append
    path every state mutator uses (:func:`_commit_worktree_state`), so the
    single-writer invariant holds and the board re-renders the new question on
    its next refresh.

    Args:
        ctx: Server context -- must carry ``state_path`` (+ ``event_path`` /
            ``wal_dir`` for the canonical write).
        params: JSON-RPC params per :class:`AddQuestionParams`.

    Returns:
        Dict matching :class:`AddQuestionResult`.

    Raises:
        ValueError: When *params* does not validate against
            :class:`AddQuestionParams` (an unknown key, an empty / over-cap
            title). Mapped to ``-32602 invalid params``.
    """
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    args = AddQuestionParams.model_validate(params)
    repo_root = Path(args.repo_root) if args.repo_root else None
    question_id = args.question_id if args.question_id is not None else f"OQ-{uuid.uuid4().hex[:8]}"
    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=params,
        command="research.add_question",
        scope_id=args.scope_id,
        apply_func=lambda state: _apply_add_question(state, args, question_id=question_id),
    )


# --------------------------------------------------------------------------
# Round-end claim reconcile -- fold a round's findings into Claim rows
# --------------------------------------------------------------------------


def reconcile_round_claims(
    state: State,
    findings: RoundFindings,
    *,
    scope_id: str | None,
    now: datetime,
) -> list[str]:
    """Fold one round's parsed findings into ``state.open`` Claim rows.

    The round-end reconcile: every finding line a round's spawned researchers
    returned (:attr:`~eawf.kernel.spec.campaign_driver.RoundFindings.finding_lines`)
    becomes one ``OPEN`` :class:`~eawf.kernel.state.models.Claim` row carrying
    the body's ``evidence_refs`` as the claim's evidence (so the EviBound
    resolver + the saturation reducer score real rows). The claim id is
    ``CLM-r<round>-<domain>-<n>`` so a re-run round does not collide. A finding
    line over the title bound is truncated to the 72-char title cap with its
    full text preserved in the description.

    Pure with respect to its inputs aside from mutating *state* in place (the
    caller owns the canonical persist). Returns the ids of the claims written so
    the caller can record the per-round claim count.

    Args:
        state: Loaded :class:`State`. ``state.claims`` is mutated in place.
        findings: The round's parsed findings.
        scope_id: Explicit scope for the claims; ``None`` resolves to the
            project code.
        now: The instant the claims are logged at.

    Returns:
        The ids of the Claim rows written this round, in finding order.
    """
    from eawf.kernel.state.models import Claim

    resolved_scope = _resolve_research_scope(state, scope_id)
    claims = dict(state.claims or {})
    written: list[str] = []
    for domain, body in zip(findings.domains, findings.bodies, strict=True):
        evidence = [ref.ref for ref in body.evidence_refs]
        for index, line in enumerate(body.findings):
            claim_id = f"CLM-r{findings.round_number}-{domain}-{index}"
            title = line if len(line) <= 72 else f"{line[:69]}..."
            description = None if len(line) <= 72 else line[:500]
            claims[claim_id] = Claim(
                id=claim_id,
                scope_id=resolved_scope,
                title=title,
                description=description,
                status=ClaimStatus.OPEN,
                evidence_refs=evidence,
                created_at=now,
            )
            written.append(claim_id)
    state.claims = claims
    logger.info(
        f"reconcile_round_claims round={findings.round_number} scope={resolved_scope!r} "
        f"claims={len(written)}"
    )
    return written


# --------------------------------------------------------------------------
# research.run -- drive a campaign run, persist each round + checkpoint
# --------------------------------------------------------------------------


def persist_round(state_path: Path, payload: ResearchRoundPayload) -> str:
    """Append one :class:`ResearchRoundPayload` to the ``research_round`` store.

    Wraps the payload in an :class:`Envelope` with
    ``kind=StoreKind.RESEARCH_ROUND`` and appends it via
    :func:`~eawf.kernel.store.append.append_envelope` (per-file portalock +
    fsync). The round store is append-only and non-state, the same as the
    campaign store; the board re-reads it to render the RUN / ROUND bands.

    Args:
        state_path: Path to the scope's ``state.json``; the round store
            resolves under its sibling ``store/`` directory.
        payload: The validated round payload to persist.

    Returns:
        The appended envelope id (``<campaign_id>-r<round_number>``).
    """
    envelope_id = f"{payload.campaign_id}-r{payload.round_number}"
    envelope = Envelope(
        id=envelope_id,
        kind=StoreKind.RESEARCH_ROUND,
        scope_id=None,
        created_at=payload.recorded_at,
        summary=f"round {payload.round_number} of {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, StoreKind.RESEARCH_ROUND), envelope)
    return envelope_id


def read_campaign_rounds(state_path: Path, campaign_id: str) -> list[ResearchRoundPayload]:
    """Return every persisted round for *campaign_id*, in round order.

    Walks the append-only ``research_round`` store under *state_path*,
    validating each envelope + payload, and returns the rows matching
    *campaign_id* sorted by round number. Empty when the store is absent or
    carries no row for the id (the common pre-run path).

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The campaign whose rounds to read.

    Returns:
        The campaign's round payloads in ascending round order.
    """
    path = store_path(state_path, StoreKind.RESEARCH_ROUND)
    if not path.exists():
        return []
    rounds: list[ResearchRoundPayload] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchRoundPayload.model_validate(envelope.payload)
        if payload.campaign_id == campaign_id:
            rounds.append(payload)
    rounds.sort(key=lambda r: r.round_number)
    return rounds


class RunCampaignParams(BaseModel):
    """Params for :func:`run`.

    Drives a bounded campaign run over the persisted staged campaign. The run
    spawns a researcher session per staged dispatch each round (the W01 round
    runner), reconciles the round's findings into Claim rows, persists the
    round + checkpoint, and halts on the first of saturation or the round
    budget.

    Attributes:
        campaign_id: The persisted campaign to run; must name an ACTIVE
            campaign already in the store.
        round_budget: Hard ceiling on rounds (>= 1). Defaults to the
            canonical :data:`~eawf.kernel.spec.round_loop.DEFAULT_ROUND_BUDGET`.
        scope_id: Explicit scope the round's claims bind to; ``None`` resolves
            to the active project's code.
        repo_root: Optional per-request repo anchor; ``None`` uses the
            daemon's boot-time state path.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)
    round_budget: int = Field(default=DEFAULT_ROUND_BUDGET, ge=1)
    scope_id: str | None = None
    repo_root: str | None = None


class RunCampaignResult(BaseModel):
    """Result of :func:`run`.

    Attributes:
        campaign_id: The campaign that was run.
        rounds_run: How many rounds the bounded loop executed.
        halt_reason: Why the loop stopped (``saturated`` / ``round_budget``).
        saturated: Whether the run ended because the campaign converged.
        checkpoints: How many operator-review checkpoints the run recorded.
        claim_ids: The Claim row ids written across every round.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    rounds_run: int
    halt_reason: str
    saturated: bool
    checkpoints: int
    claim_ids: list[str]


class ResearchRunHandle(BaseModel):
    """Run handle returned by the backgrounded ``research.run`` RPC.

    Attributes:
        handle_id: Opaque id for correlating daemon logs with this run.
        campaign_id: Campaign id being drained in the background.
        run_state: State at the moment the RPC returned.
        backgrounded: True when a worker thread was started.
    """

    model_config = ConfigDict(extra="forbid")
    handle_id: str
    campaign_id: str
    run_state: str
    backgrounded: bool


class _ThreadsafeBus:
    """Marshal bus publishes from a research worker back onto the daemon loop."""

    def __init__(
        self,
        bus: Any,
        *,
        loop: asyncio.AbstractEventLoop | None,
        loop_thread: threading.Thread,
    ) -> None:
        self._bus = bus
        self._loop = loop
        self._loop_thread = loop_thread

    @property
    def active_subscriptions(self) -> int:
        return int(getattr(self._bus, "active_subscriptions", 0))

    def publish(self, envelope: Envelope) -> None:
        if (
            self._loop is not None
            and self._loop.is_running()
            and threading.current_thread() is not self._loop_thread
        ):
            self._loop.call_soon_threadsafe(self._bus.publish, envelope)
            return
        self._bus.publish(envelope)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)


class _ActiveResearchRun:
    """Process-local bookkeeping for one background campaign run."""

    def __init__(
        self,
        *,
        handle_id: str,
        campaign_id: str,
        thread: threading.Thread,
    ) -> None:
        self.handle_id = handle_id
        self.campaign_id = campaign_id
        self.thread = thread
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None


_RESEARCH_RUN_LOCK = threading.Lock()
_ACTIVE_RESEARCH_RUNS: dict[str, _ActiveResearchRun] = {}


def research_run_in_flight(campaign_id: str | None = None) -> bool:
    """Return whether a background research run is active."""
    with _RESEARCH_RUN_LOCK:
        if campaign_id is not None:
            return campaign_id in _ACTIVE_RESEARCH_RUNS
        return bool(_ACTIVE_RESEARCH_RUNS)


def _live_agent_end_producer(ctx: MethodContext, runtime: str) -> AgentEndProducer:
    """Build the production agent-end producer over the ``agent.dispatch`` spawn.

    Each staged dispatch becomes a real spawned researcher session: the
    producer drives the daemon ``agent.dispatch`` spawn=True path (which
    registers a session behind the safety floor, spawns the child, and binds
    the spawned agent's own output to a typed report body) and returns the
    spawned researcher's decoded ``agent_end`` body. This is the live wiring;
    a test injects a stub producer into :func:`run_campaign` instead, so no
    real subprocess is spawned under test.

    Args:
        ctx: The daemon context the spawn runs under (needs state + event
            paths for the live spawn).
        runtime: The runtime adapter the researcher spawns on.

    Returns:
        An :data:`AgentEndProducer` that spawns one researcher per dispatch.
    """

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        return _run_research_spawn_threaded(ctx, runtime=runtime, dispatch=dispatch)

    return _produce


def _run_research_spawn_threaded(
    ctx: MethodContext,
    *,
    runtime: str,
    dispatch: StagedDispatch,
) -> Mapping[str, object]:
    """Run one live researcher spawn from the synchronous round-runner seam."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    async def _spawn() -> Mapping[str, object]:
        return await _spawn_researcher_agent_end(ctx, runtime=runtime, dispatch=dispatch)

    def _run() -> Mapping[str, object]:
        return asyncio.run(_spawn())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def _append_researcher_event(
    ctx: MethodContext,
    *,
    phase: str,
    dispatch: StagedDispatch,
    scope_id: str,
) -> None:
    """Append a researcher spawn / finish lifecycle marker for the Feed pane.

    Mirrors :func:`_append_research_run_round_event`: a store-less context (no
    ``event_path`` -- a stateless unit test) is a no-op, otherwise the row is
    appended through the session event store + fanned on the bus so the Feed
    shows a researcher spawn + finish row per domain dispatch.

    Args:
        ctx: Daemon method context -- supplies ``event_path`` + ``bus``.
        phase: ``"spawn"`` or ``"finish"``.
        dispatch: The staged dispatch the researcher runs (names the domain).
        scope_id: The researcher session scope the row is keyed on.
    """
    if ctx.event_path is None:
        return
    now = datetime.now(UTC)
    event = append_event(
        events_path=Path(ctx.event_path),
        event_id=f"EV-researcher-{phase}-{uuid.uuid4().hex[:12]}",
        event_type=f"research.researcher.{phase}",
        actor="daemon",
        command="research.run",
        args_hash="",
        status="ok",
        message=f"researcher {phase} domain={dispatch.domain!r} scope={scope_id}",
        scope_id=scope_id,
        occurred_at=now,
    )
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(event)
    ctx.last_event_id = event.id


async def _spawn_researcher_agent_end(
    ctx: MethodContext,
    *,
    runtime: str,
    dispatch: StagedDispatch,
) -> Mapping[str, object]:
    """Spawn one researcher and return its validated ``agent_end`` body.

    This is the research-campaign analogue of the live ``agent.dispatch``
    spawn path: it registers a role-typed session, spawns through the selected
    runtime adapter, validates the spawned output through the same bounded
    schema-assist loop, prices the spawn, and drives the dispatch runner so
    dispatch-cost / agent-output / role-specific ``agent_end`` evidence lands
    in the stores.
    """
    if ctx.state_path is None or ctx.event_path is None:
        raise RuntimeError("research.run live spawn requires state_path + event_path")
    state_path = Path(ctx.state_path)
    serving_runtime = _canonical_runtime(runtime)
    runtime_triple = _runtime_triple(serving_runtime)
    wave_id = _active_research_wave_id(state_path)
    scope_id = f"{wave_id}-research-{uuid.uuid4().hex[:8]}"
    session_id = _register_researcher_session(
        ctx,
        scope_id=scope_id,
        runtime=serving_runtime,
    )
    prompt = _researcher_prompt(dispatch)
    repo_root = state_path.parent.parent
    model = model_for_runtime(
        AgentSessionRole.RESEARCHER,
        _effort_for_depth(dispatch.depth.value),
        runtime_triple,
        runtime_models=resolve_runtime_tier_models(repo_root),
    )
    adapter = select_adapter(serving_runtime)
    captured_pid: list[int] = []

    # Feed lifecycle marker: a researcher spawn row so the Feed shows the
    # campaign's researcher dispatch, mirroring the wave-spawn lifecycle rows.
    _append_researcher_event(ctx, phase="spawn", dispatch=dispatch, scope_id=scope_id)

    # Stream the researcher's stdout live to the Watch tail (mirroring the
    # wave-spawn chunk wiring): batch lines + persist each batch as an
    # agent.output.chunk keyed on the wave so the Watch renders it live rather
    # than only a terminal agent.output at completion.
    chunk_buffer: list[str] = []
    chunk_seq = [0]

    def _flush_chunk_buffer() -> None:
        if not chunk_buffer:
            return
        emit_agent_output_chunk(
            ctx,
            wave_id=wave_id,
            session_id=session_id,
            seq=chunk_seq[0],
            text="".join(chunk_buffer),
        )
        chunk_seq[0] += 1
        chunk_buffer.clear()

    async def _on_chunk(line: str) -> None:
        chunk_buffer.append(line)
        if len(chunk_buffer) >= _CHUNK_BATCH_LINES:
            _flush_chunk_buffer()

    spawn_result = await adapter.spawn_session(
        prompt,
        model=model,
        cwd=str(repo_root),
        on_spawn=captured_pid.append,
        on_chunk=_on_chunk,
    )

    spawns = 0

    async def _assist_spawn(reask_prompt: str) -> Any:
        nonlocal spawns
        spawns += 1
        if spawns == 1:
            return spawn_result
        return await adapter.spawn_session(
            reask_prompt,
            model=spawn_result.model,
            cwd=str(repo_root),
            on_spawn=captured_pid.append,
            on_chunk=_on_chunk,
        )

    assist = await assist_with_schema(
        prompt,
        spawn=_assist_spawn,
        validator=ResearcherReportBody.model_validate,
    )
    # Final flush: empty any partial batch so the persisted researcher tail is
    # complete + emit the finish lifecycle marker for the Feed.
    _flush_chunk_buffer()
    _append_researcher_event(ctx, phase="finish", dispatch=dispatch, scope_id=scope_id)
    body = assist.body
    if not isinstance(body, ResearcherReportBody):  # pragma: no cover - validator narrows
        raise TypeError(f"assist returned non-researcher body: {body.role!r}")

    metered = price_spawn_result(spawn_result)
    tokens = DispatchTokens(
        input_tokens=metered.input_tokens,
        output_tokens=metered.output_tokens,
        cache_creation_input_tokens=metered.cache_creation_input_tokens,
        cache_read_input_tokens=metered.cache_read_input_tokens,
    )
    pid = captured_pid[-1] if captured_pid else spawn_result.subprocess_pid
    run_dispatch(
        ctx,
        wave_id=wave_id,
        primary_runtime=runtime_triple,
        fallback_runtime=runtime_triple,
        model=metered.model,
        pricing_version=metered.pricing_version,
        primary_error=None,
        tokens=tokens,
        cost_usd=metered.cost_usd,
        session_id=session_id,
        report_body=body,
        pgid=pid,
        enforce=_resolve_budget_enforce(state_path),
        output_text=spawn_result.text,
    )
    logger.info(
        f"_spawn_researcher_agent_end wave={wave_id} domain={dispatch.domain!r} "
        f"runtime={serving_runtime!r} session={session_id!r} findings={len(body.findings)}"
    )
    return body.model_dump(mode="json")


def _canonical_runtime(runtime: str) -> str:
    """Return the canonical runtime id accepted by runtime selectors."""
    try:
        return _RUNTIME_ALIASES[runtime]
    except KeyError as exc:
        known = ", ".join(sorted(_RUNTIME_ALIASES))
        raise ValueError(f"unknown runtime: {runtime!r} (known: {known})") from exc


def _runtime_triple(runtime: str) -> RuntimeTriple:
    """Return the event-surface runtime spelling for *runtime*."""
    try:
        return _RUNTIME_TRIPLES[runtime]
    except KeyError as exc:
        known = ", ".join(sorted(_RUNTIME_TRIPLES))
        raise ValueError(f"unknown runtime: {runtime!r} (known: {known})") from exc


def _effort_for_depth(depth: str) -> EffortBucket:
    """Map campaign survey depth onto the runtime routing effort bucket."""
    if depth == "shallow":
        return EffortBucket.S
    if depth == "deep":
        return EffortBucket.L
    if depth == "exhaustive":
        return EffortBucket.XL
    return EffortBucket.M


def _active_research_wave_id(state_path: Path) -> str:
    """Resolve the active wave that should accrue a live campaign's cost."""
    state = load_state(state_path)
    for wave_id in reversed(state.current.active_wave_ids):
        if wave_id in state.waves:
            return wave_id
    if state.current.iter_id is not None:
        it = state.iters.get(state.current.iter_id)
        if it is not None:
            for wave_id in reversed(it.wave_ids):
                if wave_id in state.waves:
                    return wave_id
    raise RuntimeError("research.run live spawn requires an active wave")


def _researcher_prompt(dispatch: StagedDispatch) -> str:
    """Render the researcher prompt with a strict ``agent_end`` JSON contract."""
    return (
        f"{dispatch.prompt}\n\n"
        f"Research domain: {dispatch.domain}\n"
        f"Depth: {dispatch.depth.value}\n\n"
        "Return only a JSON object matching this agent_end schema:\n"
        '{"role":"researcher","verdict":"pass|pass-with-followups|fail|blocked",'
        '"confidence":"low|medium|high","summary":"...","question":"...",'
        '"findings":["..."],"recommendation":"...",'
        '"evidence_refs":[{"kind":"store_record","ref":"repo-relative ref"}]}\n'
        "Use repo-relative or external evidence refs only."
    )


def _register_researcher_session(
    ctx: MethodContext,
    *,
    scope_id: str,
    runtime: str,
) -> str:
    """Register an ACTIVE researcher session through the canonical state writer."""
    if ctx.state_path is None or ctx.event_path is None:
        raise RuntimeError("research.run live spawn requires state_path + event_path")
    state_path = Path(ctx.state_path)
    events_path = Path(ctx.event_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        before_version = state_version(state.model_dump(mode="json"))
        try:
            result = start_session(
                state=state,
                events_path=events_path,
                role=AgentSessionRole.RESEARCHER,
                scope_id=scope_id,
                runtime=runtime,
            )
        except SessionConflict as exc:
            raise RuntimeError(f"researcher session collision: {scope_id!r}") from exc
        session_id = result.session.id
        state.updated_at = datetime.now(UTC)
        new_payload = state.model_dump(mode="json")
        after_version = state_version(new_payload)
        atomic_write_json_locked(state_path, new_payload)
    logger.info(
        f"_register_researcher_session scope_id={scope_id!r} runtime={runtime!r} "
        f"session={session_id!r} before={before_version} after={after_version}"
    )
    return session_id


def _resolve_budget_enforce(state_path: Path) -> EnforceMode:
    """Resolve ``flow.budget.enforce`` for the repo that owns ``state_path``."""
    repo = state_path.parent.parent
    merged, _sources = merge_config(workspace=repo, repo=repo)
    flow = merged.get("flow")
    budget = flow.get("budget") if isinstance(flow, dict) else None
    value = budget.get("enforce", DEFAULT_ENFORCE) if isinstance(budget, dict) else DEFAULT_ENFORCE
    if value not in ("soft", "hard"):
        raise ValueError(f"invalid flow.budget.enforce: {value!r}")
    return cast(EnforceMode, value)


def _validate_runnable_campaign(state_path: Path, campaign_id: str) -> None:
    """Raise when *campaign_id* cannot be started as an ACTIVE campaign."""
    campaign = read_latest_campaign(state_path, campaign_id)
    if campaign is None:
        raise ValueError(f"unknown campaign: {campaign_id!r}")
    if campaign.status is not CampaignStatus.ACTIVE:
        raise ValueError(f"campaign not active: {campaign_id!r} is {campaign.status.value!r}")


def _research_worker_context(
    ctx: MethodContext,
    *,
    loop: asyncio.AbstractEventLoop | None,
    loop_thread: threading.Thread,
) -> MethodContext:
    """Return a worker-safe context for background ``research.run``."""
    if ctx.bus is None or not hasattr(ctx.bus, "publish"):
        return ctx
    return replace(
        ctx,
        bus=_ThreadsafeBus(ctx.bus, loop=loop, loop_thread=loop_thread),
    )


def start_background_research_run(
    ctx: MethodContext,
    args: RunCampaignParams,
    *,
    produce_agent_end: AgentEndProducer,
    checkpoint_policy: CheckpointPolicy | None = None,
) -> ResearchRunHandle:
    """Start ``run_campaign`` on a worker thread and return a run handle.

    ``run_campaign`` is synchronous because each round blocks on live researcher
    spawns. Running it inside the awaited RPC handler would monopolise the
    daemon event loop, so the RPC validates the campaign, records a process-local
    handle, and lets a daemon thread drain the bounded campaign while concurrent
    RPCs such as ``daemon.ping`` and ``research.steer`` continue to answer.

    Args:
        ctx: Daemon context.
        args: Validated run params.
        produce_agent_end: Per-dispatch agent-end producer.
        checkpoint_policy: Optional checkpoint cadence.

    Returns:
        The handle for the started background run.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset.
        ValueError: When the campaign is unknown, inactive, or already running.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    _validate_runnable_campaign(state_path, args.campaign_id)
    with _RESEARCH_RUN_LOCK:
        if args.campaign_id in _ACTIVE_RESEARCH_RUNS:
            raise ValueError(f"research run already in flight: {args.campaign_id!r}")

    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    loop_thread = threading.current_thread()
    handle_id = f"research-run-{uuid.uuid4().hex[:12]}"
    active_holder: list[_ActiveResearchRun] = []

    def _drive() -> None:
        active = active_holder[0]
        worker_ctx = _research_worker_context(ctx, loop=loop, loop_thread=loop_thread)
        try:
            active.result = run_campaign(
                worker_ctx,
                args,
                produce_agent_end=produce_agent_end,
                checkpoint_policy=checkpoint_policy,
            )
        except Exception as exc:  # pragma: no cover - defensive background guard
            active.error = exc
            logger.exception(f"start_background_research_run failed handle={handle_id!r}")
        finally:
            with _RESEARCH_RUN_LOCK:
                current = _ACTIVE_RESEARCH_RUNS.get(args.campaign_id)
                if current is active:
                    _ACTIVE_RESEARCH_RUNS.pop(args.campaign_id, None)
            logger.info(f"start_background_research_run done handle={handle_id!r}")

    thread = threading.Thread(target=_drive, name=f"research-run-{handle_id}", daemon=True)
    active = _ActiveResearchRun(
        handle_id=handle_id,
        campaign_id=args.campaign_id,
        thread=thread,
    )
    active_holder.append(active)
    with _RESEARCH_RUN_LOCK:
        if args.campaign_id in _ACTIVE_RESEARCH_RUNS:
            raise ValueError(f"research run already in flight: {args.campaign_id!r}")
        _ACTIVE_RESEARCH_RUNS[args.campaign_id] = active
    thread.start()
    logger.info(
        f"start_background_research_run started handle={handle_id!r} campaign={args.campaign_id!r}"
    )
    return ResearchRunHandle(
        handle_id=handle_id,
        campaign_id=args.campaign_id,
        run_state="running",
        backgrounded=True,
    )


def _emit_research_run_round_event(
    ctx: MethodContext,
    *,
    campaign_id: str,
    round_number: int,
    claim_ids: list[str],
    saturated: bool,
    checkpoint: bool,
) -> None:
    """Append a smoke evidence event for a persisted ``research.run`` round."""
    if ctx.event_path is None:
        return
    now = datetime.now(UTC)
    event = append_event(
        events_path=Path(ctx.event_path),
        event_id=f"EV-research-run-{uuid.uuid4().hex[:12]}",
        event_type="research.run.round",
        actor="daemon",
        command="research.run",
        args_hash="",
        status="ok",
        message=(
            f"research.run round={round_number} campaign={campaign_id} "
            f"claims={len(claim_ids)} saturated={saturated} checkpoint={checkpoint}"
        ),
        scope_id=campaign_id,
        occurred_at=now,
    )
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(event)
    ctx.last_event_id = event.id


def _reconcile_round_claims_for(
    ctx: MethodContext,
    findings: RoundFindings,
    *,
    state_path: Path,
    campaign_id: str,
    scope_id: str | None,
    now: datetime,
    fold_into_state: bool,
) -> list[Claim]:
    """Reconcile a round's findings into Claim rows, folding into real state.

    When *fold_into_state* is true the reconcile runs inside
    :func:`~eawf.runtime.daemon.methods.state._commit_worktree_state` so the new
    Claim rows land on the canonical ``state.claims`` through the daemon-owned
    per-file portalock + WAL + event-append path (AGENTS rule 4) -- the same
    writer ``add_question`` uses for ``state.open_questions`` -- and the rows are
    read back off the post-write state. When false (the run-rpc unit path with no
    on-disk state / WAL dir) the reconcile runs against a throwaway in-memory
    shadow so the run still completes and yields the round's Claim rows.

    Args:
        ctx: The daemon context the canonical write runs under.
        findings: The round's parsed findings.
        state_path: Path to the scope's ``state.json``.
        campaign_id: The running campaign id (recorded in the event params).
        scope_id: Explicit scope for the claims; ``None`` resolves to the project.
        now: The instant the claims are logged at.
        fold_into_state: Whether the canonical-writer fold path is available.

    Returns:
        The Claim rows written this round, in finding order.
    """
    from eawf.kernel.state.models import State

    if not fold_into_state:
        shadow = State.model_construct(claims={}, open_questions={}, project=None)
        reconcile_round_claims(shadow, findings, scope_id=scope_id, now=now)
        return list((shadow.claims or {}).values())

    from eawf.runtime.daemon.methods.state import _commit_worktree_state
    from eawf.workflow.evidence._io import load_state

    written_ids: list[str] = []

    def _apply(state: State) -> dict[str, Any]:
        written_ids.extend(reconcile_round_claims(state, findings, scope_id=scope_id, now=now))
        return {"claim_ids": list(written_ids)}

    _commit_worktree_state(
        ctx=ctx,
        repo_root=None,
        params={"campaign_id": campaign_id, "round_number": findings.round_number},
        command="research.run.reconcile_round",
        scope_id=scope_id,
        apply_func=_apply,
    )
    claims = load_state(state_path).claims or {}
    return [claims[cid] for cid in written_ids if cid in claims]


def _prune_carried_ledger(
    carried_claims: list[Claim],
    round_claims: list[Claim],
    *,
    campaign_id: str,
    now: datetime,
) -> None:
    """Carry the round's claims forward and prune the ledger to the live frontier.

    Appends *round_claims* to the accumulated *carried_claims* ledger, then runs
    the L1 between-rounds reducer (:func:`~eawf.kernel.spec.pruning.prune_round_carryover`)
    over it so the provably-dead rows (``SUPERSEDED`` + answers-to-``DROPPED``-
    questions) drop. The ledger is trimmed in place to the kept (live) frontier
    the next round carries.

    Args:
        carried_claims: The accumulated ledger, mutated in place.
        round_claims: The Claim rows this round reconciled.
        campaign_id: The running campaign id (for the log line).
        now: The reference instant threaded into the reducer.
    """
    carried_claims.extend(round_claims)
    pruned = prune_round_carryover(carried_claims, [], now=now)
    kept_ids = set(pruned.kept)
    carried_claims[:] = [claim for claim in carried_claims if claim.id in kept_ids]
    logger.info(
        f"_prune_carried_ledger campaign={campaign_id!r} "
        f"kept={len(pruned.kept)} dropped={len(pruned.dropped)}"
    )


def run_campaign(
    ctx: MethodContext,
    args: RunCampaignParams,
    *,
    produce_agent_end: AgentEndProducer,
    checkpoint_policy: CheckpointPolicy | None = None,
) -> dict[str, Any]:
    """Drive a bounded campaign run, persisting each round + reconciling claims.

    The engine behind :func:`run`: it loads the persisted campaign, builds the
    W01 round runner over the injected *produce_agent_end* spawn, drives the
    bounded loop via :func:`~eawf.kernel.spec.campaign_driver.drive_campaign`
    at the live cockpit level, and after each round reconciles the round's
    findings into Claim rows + persists a :class:`ResearchRoundPayload`. The
    run respects *args.round_budget* (the hard ceiling) and the saturation
    gates (the loop halts on the first of saturation or budget).

    The spawn is injected so the whole run is unit-testable under a stub
    producer + fixture ``agent_end`` bodies (no live subprocess).

    Args:
        ctx: Daemon context (needs ``state_path``).
        args: Validated :class:`RunCampaignParams`.
        produce_agent_end: The per-dispatch agent-end producer (live spawn in
            production; a fixture stub under test).
        checkpoint_policy: Operator-review cadence for the live run; ``None``
            uses the autonomous ON_HALT default.

    Returns:
        Dict matching :class:`RunCampaignResult`.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset.
        ValueError: When the campaign id names no ACTIVE campaign.
        ResearcherDispatchError: Propagated when a round's spawned researcher
            body fails to parse.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    campaign = read_latest_campaign(state_path, args.campaign_id)
    if campaign is None:
        raise ValueError(f"unknown campaign: {args.campaign_id!r}")
    if campaign.status is not CampaignStatus.ACTIVE:
        raise ValueError(f"campaign not active: {args.campaign_id!r} is {campaign.status.value!r}")

    # Reconcile each round's findings into Claim rows as the loop drives, so
    # the saturation reducer scores the real ledger. Each round's claims fold
    # into the canonical ``state.claims`` through the daemon-owned state writer
    # (:func:`_commit_worktree_state`, the same path ``add_question`` uses), so
    # a live run populates ``state.claims`` rather than a throwaway shadow. The
    # per-round store append + the L1 carryover prune ride alongside.
    claim_ids: list[str] = []
    round_payloads: list[ResearchRoundPayload] = []
    # The accumulated live-claim ledger carried between rounds. The L1 carryover
    # reducer (:func:`prune_round_carryover`) runs over it after each round so
    # the next round + the synthesis work over only the live claims.
    carried_claims: list[Claim] = []
    # The canonical state writer needs the on-disk state + a WAL dir. The
    # run-rpc unit path drives run_campaign against a context with neither (no
    # real ``.ea/state.json``, no ``wal_dir``); there the run still completes and
    # accumulates the round's claim ids in-memory, but no state fold occurs.
    can_fold_state = state_path.exists() and isinstance(ctx.wal_dir, Path)

    def _channel_notes() -> tuple[list[str], bool]:
        """Fold the operator channel: return the active steer notes + paused bit.

        Read before each round so a mid-run steer / override lands on the NEXT
        round's record -- the active queued steers + the effective locked
        overrides shape the round's dispatch set (D-3), and a blocking input
        soft-pauses the round (D-2, surfaced as the ``paused`` bit).
        """
        fold = fold_operator_channel(state_path, args.campaign_id)
        notes: list[str] = [
            inp.note
            for inp in fold.queued
            if inp.kind in (OperatorInputKind.STEER, OperatorInputKind.NOTICE_BROADCAST)
        ]
        notes.extend(f"override[{eo.scope}]={eo.value}" for eo in fold.effective_overrides)
        return notes, fold.paused

    def _reconcile(findings: RoundFindings) -> tuple[list[str], bool]:
        now = datetime.now(UTC)
        steer_notes, paused = _channel_notes()
        round_claims = _reconcile_round_claims_for(
            ctx,
            findings,
            state_path=state_path,
            campaign_id=args.campaign_id,
            scope_id=args.scope_id,
            now=now,
            fold_into_state=can_fold_state,
        )
        written = [claim.id for claim in round_claims]
        claim_ids.extend(written)
        # Carry the round's claims forward, then run the L1 carryover reducer over
        # the accumulated ledger so the next round + the synthesis work over only
        # the live claims (dead rows drop). The kept set is the live frontier the
        # next round carries.
        _prune_carried_ledger(carried_claims, round_claims, campaign_id=args.campaign_id, now=now)
        payload = ResearchRoundPayload(
            campaign_id=args.campaign_id,
            round_number=findings.round_number,
            domains=list(findings.domains),
            finding_lines=list(findings.finding_lines),
            claim_ids=written,
            saturated=False,
            checkpoint=False,
            steer_notes=steer_notes,
            recorded_at=now,
        )
        round_payloads.append(payload)
        return steer_notes, paused

    def _saturation(findings: RoundFindings) -> Any:
        from eawf.kernel.spec.saturation import SaturationReport

        _steer_notes, paused = _reconcile(findings)
        # A blocking operator input (D-2) soft-pauses the round: the loop halts
        # as if saturated so the run yields to the operator. Otherwise a round
        # with findings stays not-dry so the loop runs to the budget (the real
        # ledger fold lands with W05/W06).
        dry = paused or not findings.finding_lines
        return SaturationReport(
            saturated=dry,
            gates=(),
            live_claim_count=len(findings.finding_lines),
            empty_ledger=not findings.finding_lines,
        )

    spawner: DispatchSpawner = build_live_dispatch_spawner(produce_agent_end)
    # The runner records its own RoundFindings list, but run_campaign builds the
    # richer per-round payloads inside _reconcile (folding in the claim ids), so
    # the runner's bare list is intentionally unused here.
    runner, _runner_rounds = build_round_runner(campaign.campaign, spawner, _saturation)
    policy = (
        checkpoint_policy
        if checkpoint_policy is not None
        else CheckpointPolicy(tier=CheckpointTier.ON_HALT)
    )
    result = drive_campaign(
        campaign.campaign.topic,
        campaign.config,
        level=CockpitLevel.LIVE,
        round_runner=runner,
        round_budget=args.round_budget,
        checkpoint_policy=policy,
    )
    loop = result.loop_result
    assert loop is not None  # a live drive always runs the loop

    # Persist each round, stamping the saturated bit + whether the round
    # coincided with a recorded checkpoint (the terminal round under ON_HALT).
    checkpoint_rounds = {cp.round_number for cp in loop.checkpoints}
    for payload in round_payloads:
        stamped = payload.model_copy(
            update={
                "saturated": payload.round_number == loop.rounds_run and loop.saturated,
                "checkpoint": payload.round_number in checkpoint_rounds,
            }
        )
        persist_round(state_path, stamped)
        _emit_research_run_round_event(
            ctx,
            campaign_id=args.campaign_id,
            round_number=stamped.round_number,
            claim_ids=list(stamped.claim_ids),
            saturated=stamped.saturated,
            checkpoint=stamped.checkpoint,
        )
    logger.info(
        f"run_campaign campaign={args.campaign_id!r} rounds={loop.rounds_run} "
        f"halt={loop.halt_reason.value} checkpoints={len(loop.checkpoints)} "
        f"claims={len(claim_ids)}"
    )
    return RunCampaignResult(
        campaign_id=args.campaign_id,
        rounds_run=loop.rounds_run,
        halt_reason=loop.halt_reason.value,
        saturated=loop.saturated,
        checkpoints=len(loop.checkpoints),
        claim_ids=claim_ids,
    ).model_dump(mode="json")


@register("research.run")
async def run(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Start a bounded research campaign over the live ``agent.dispatch`` spawn.

    Registers the campaign run RPC over
    :func:`~eawf.kernel.spec.campaign_driver.drive_campaign` with the
    production-bound round runner, but returns a run handle immediately and
    drives the blocking campaign loop on a worker thread. Each round still
    spawns a researcher session per staged dispatch, reconciles the findings
    into Claim rows, and persists the round + checkpoint; the board RUN band
    reads those persisted rounds as the background thread advances.

    Args:
        ctx: Server context -- needs ``state_path`` (+ ``event_path`` for the
            live spawn).
        params: JSON-RPC params per :class:`RunCampaignParams`.

    Returns:
        Dict matching :class:`ResearchRunHandle`.

    Raises:
        ValueError: When *params* does not validate or the campaign id names
            no ACTIVE campaign. Mapped to ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = RunCampaignParams.model_validate(params)
    # Production binds the live agent.dispatch spawn; the binding-pass test
    # harness injects a stub producer into run_campaign directly, so this RPC
    # entrypoint never spawns a real subprocess under test.
    produce_agent_end = _live_agent_end_producer(ctx, runtime="claude")
    handle = start_background_research_run(
        ctx,
        args,
        produce_agent_end=produce_agent_end,
    )
    return handle.model_dump(mode="json")


# --------------------------------------------------------------------------
# research.followup / research.snapshot -- typed read RPCs over a run
# --------------------------------------------------------------------------


class FollowupParams(BaseModel):
    """Params for :func:`followup`.

    Attributes:
        campaign_id: The campaign to queue a follow-up against; must name a
            stored campaign.
        note: Optional free-text follow-up note recorded in the result.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=500)


class FollowupResult(BaseModel):
    """Result of :func:`followup`.

    Attributes:
        campaign_id: The campaign the follow-up targets.
        rounds_run: How many rounds the campaign has run so far.
        next_round: The round number a follow-up run would start at.
        note: The echoed follow-up note, or ``None``.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    rounds_run: int
    next_round: int
    note: str | None


@register("research.followup")
async def followup(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer a follow-up query over a campaign's persisted rounds.

    The honest follow-up surface the board's ``r`` key routes to: it reports
    how many rounds the campaign has run and the round a follow-up would start
    at, reading the persisted round store. No spawn happens here -- queuing the
    actual follow-up run is :func:`run`'s job; this names the next round.

    Args:
        ctx: Server context -- needs ``state_path``.
        params: JSON-RPC params per :class:`FollowupParams`.

    Returns:
        Dict matching :class:`FollowupResult`.

    Raises:
        ValueError: When *params* does not validate or the campaign is unknown.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = FollowupParams.model_validate(params)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    if read_latest_campaign(state_path, args.campaign_id) is None:
        raise ValueError(f"unknown campaign: {args.campaign_id!r}")
    rounds = read_campaign_rounds(state_path, args.campaign_id)
    rounds_run = len(rounds)
    return FollowupResult(
        campaign_id=args.campaign_id,
        rounds_run=rounds_run,
        next_round=rounds_run + 1,
        note=args.note,
    ).model_dump(mode="json")


class SnapshotParams(BaseModel):
    """Params for :func:`snapshot`.

    Attributes:
        campaign_id: The campaign to snapshot; must name a stored campaign.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str = Field(min_length=1)


class SnapshotResult(BaseModel):
    """Result of :func:`snapshot`.

    Attributes:
        campaign_id: The snapshotted campaign.
        status: The campaign's lifecycle status.
        topic: The campaign topic.
        rounds_run: How many rounds the campaign has run.
        saturated: Whether the latest round converged.
        checkpoints: How many rounds coincided with a checkpoint.
        total_findings: Total findings lines across every round.
        total_claims: Total Claim ids written across every round.
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    status: str
    topic: str
    rounds_run: int
    saturated: bool
    checkpoints: int
    total_findings: int
    total_claims: int


@register("research.snapshot")
async def snapshot(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer a typed snapshot of a campaign's run state.

    The honest snapshot surface the board's ``s`` key routes to: it folds the
    persisted campaign + its rounds into a typed run summary (rounds run,
    saturation, checkpoint count, findings + claim totals) so a caller reads
    the real run state off the store rather than a synthetic node.

    Args:
        ctx: Server context -- needs ``state_path``.
        params: JSON-RPC params per :class:`SnapshotParams`.

    Returns:
        Dict matching :class:`SnapshotResult`.

    Raises:
        ValueError: When *params* does not validate or the campaign is unknown.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = SnapshotParams.model_validate(params)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    campaign = read_latest_campaign(state_path, args.campaign_id)
    if campaign is None:
        raise ValueError(f"unknown campaign: {args.campaign_id!r}")
    rounds = read_campaign_rounds(state_path, args.campaign_id)
    latest_saturated = rounds[-1].saturated if rounds else False
    return SnapshotResult(
        campaign_id=args.campaign_id,
        status=campaign.status.value,
        topic=campaign.campaign.topic,
        rounds_run=len(rounds),
        saturated=latest_saturated,
        checkpoints=sum(1 for r in rounds if r.checkpoint),
        total_findings=sum(len(r.finding_lines) for r in rounds),
        total_claims=sum(len(r.claim_ids) for r in rounds),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------
# Operator-channel RPCs -- steer / broadcast / override over an append-log
# --------------------------------------------------------------------------


def persist_operator_input(state_path: Path, op_input: OperatorInput) -> str:
    """Append one :class:`OperatorInput` to the ``operator_input`` store.

    The daemon-owned blackboard the hub-and-spoke control plane lands on (AGENTS
    rule 4 + the a2a verdict): every operator-initiated mid-run input is a
    single typed, append-only row the orchestrator folds. Returns the appended
    envelope id (``<campaign_id>-oi-<hex>``).

    Args:
        state_path: Path to the scope's ``state.json``.
        op_input: The validated operator input to persist.

    Returns:
        The appended envelope id.
    """
    envelope_id = f"{op_input.campaign_id}-oi-{uuid.uuid4().hex[:8]}"
    envelope = Envelope(
        id=envelope_id,
        kind=StoreKind.OPERATOR_INPUT,
        scope_id=None,
        created_at=op_input.at,
        summary=f"{op_input.kind.value} on {op_input.campaign_id}",
        payload=op_input.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, StoreKind.OPERATOR_INPUT), envelope)
    return envelope_id


def read_operator_inputs(state_path: Path, campaign_id: str) -> list[OperatorInput]:
    """Return the operator-input append-log for *campaign_id*, in append order.

    Walks the append-only ``operator_input`` store under *state_path*,
    validating each envelope + payload, and returns the rows matching
    *campaign_id* in append (chronological) order -- the order
    :meth:`~eawf.kernel.spec.operator_input.OperatorInputChannel.fold` expects.
    Empty when the store is absent or carries no row for the id.

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The campaign whose inputs to read.

    Returns:
        The campaign's operator inputs in append order.
    """
    path = store_path(state_path, StoreKind.OPERATOR_INPUT)
    if not path.exists():
        return []
    inputs: list[OperatorInput] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        op_input = OperatorInput.model_validate(envelope.payload)
        if op_input.campaign_id == campaign_id:
            inputs.append(op_input)
    return inputs


def fold_operator_channel(state_path: Path, campaign_id: str) -> ChannelFold:
    """Fold a campaign's operator-input append-log into the round-loop decisions.

    The :func:`run`-side consumer: it reads the campaign's append-log
    (:func:`read_operator_inputs`) and folds it through the pure
    :meth:`~eawf.kernel.spec.operator_input.OperatorInputChannel.fold` reducer so
    the loop sees which inputs soft-pause the round (D-2: blocking-only) and
    which locked overrides are effective (D-3: persist-locked until a later
    override on the same scope clears the lock). Pure aside from the store read.

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The campaign whose channel to fold.

    Returns:
        The :class:`~eawf.kernel.spec.operator_input.ChannelFold` for the loop.
    """
    fold = OperatorInputChannel.fold(read_operator_inputs(state_path, campaign_id))
    logger.info(
        f"fold_operator_channel campaign={campaign_id!r} blocking={len(fold.blocking)} "
        f"queued={len(fold.queued)} effective_overrides={len(fold.effective_overrides)}"
    )
    return fold


class SteerParams(BaseModel):
    """Params for :func:`steer`.

    Attributes:
        text: The steer direction note the operator typed (the board ``t``
            key sends only this).
        campaign_id: The campaign the steer targets; ``None`` records the
            steer against the most-recent ACTIVE campaign.
        action: The steer direction. Defaults to NARROW (the default the
            board's between-rounds steer carries).
        scope: The blackboard scope the steer addresses; defaults to the
            whole campaign.
        urgency: The steer's urgency rung. Defaults to NORMAL (a steer is
            between-rounds feedback, not a blocking interrupt).
    """

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    campaign_id: str | None = None
    action: SteerAction = SteerAction.NARROW
    scope: str = "campaign"
    urgency: Urgency = Urgency.NORMAL
    repo_root: str | None = None


class BroadcastParams(BaseModel):
    """Params for :func:`broadcast`.

    Attributes:
        notice: The free-text notice fanned to every running round (the board
            ``b`` key sends only this).
        campaign_id: The campaign the broadcast targets; ``None`` uses the
            most-recent ACTIVE campaign.
        scope: The blackboard scope addressed; defaults to the whole campaign.
        urgency: The broadcast's urgency rung. Defaults to NORMAL.
    """

    model_config = ConfigDict(extra="forbid")
    notice: str = Field(min_length=1)
    campaign_id: str | None = None
    scope: str = "campaign"
    urgency: Urgency = Urgency.NORMAL
    repo_root: str | None = None


class OverrideParams(BaseModel):
    """Params for :func:`override`.

    Attributes:
        verdict: The forced operator verdict value (the board ``v`` key sends
            only this).
        campaign_id: The campaign the override targets; ``None`` uses the
            most-recent ACTIVE campaign.
        scope: The blackboard scope the override pins; defaults to the whole
            campaign.
        locked: Whether the override persists-locked across rounds (D-3).
            Defaults ``True`` -- an operator override of a blocking fork is a
            fixed decision that survives subsequent rounds until cleared.
        urgency: The override's urgency rung. Defaults to URGENT so an override
            of a blocking fork blocks the round until folded (D-2).
    """

    model_config = ConfigDict(extra="forbid")
    verdict: str = Field(min_length=1)
    campaign_id: str | None = None
    scope: str = "campaign"
    locked: bool = True
    urgency: Urgency = Urgency.URGENT
    repo_root: str | None = None


class OperatorInputResult(BaseModel):
    """Result of the three operator-channel RPCs.

    Attributes:
        campaign_id: The campaign the input targeted.
        kind: The :class:`OperatorInputKind` value the input carried.
        input_id: The appended operator-input envelope id.
        blocking: Whether the input soft-pauses the round (D-2).
    """

    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    kind: str
    input_id: str
    blocking: bool


def _resolve_active_campaign_id(state_path: Path, campaign_id: str | None) -> str:
    """Resolve the campaign an operator input targets.

    An explicit *campaign_id* is required to name an ACTIVE campaign; ``None``
    is rejected because an operator channel always targets a running campaign
    (the board sends the selected campaign id). A campaign id that names no
    ACTIVE campaign is rejected so a steer / broadcast / override never lands
    against a dead or absent run.

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The caller-supplied campaign id.

    Returns:
        The resolved campaign id.

    Raises:
        ValueError: When *campaign_id* is missing or names no ACTIVE campaign.
    """
    if not campaign_id:
        raise ValueError("operator channel requires a campaign_id")
    campaign = read_latest_campaign(state_path, campaign_id)
    if campaign is None:
        raise ValueError(f"unknown campaign: {campaign_id!r}")
    if campaign.status is not CampaignStatus.ACTIVE:
        raise ValueError(f"campaign not active: {campaign_id!r} is {campaign.status.value!r}")
    return campaign_id


def _append_operator_input(
    ctx: MethodContext,
    *,
    campaign_id: str | None,
    kind: OperatorInputKind,
    scope: str,
    note: str,
    urgency: Urgency,
    payload: OverridePayload | AddQuestionPayload | SteerPayload | None,
) -> dict[str, Any]:
    """Build + persist one :class:`OperatorInput` and return the typed result.

    Shared by the steer / broadcast / override RPC handlers: resolve the target
    campaign, build the typed input (the model validator rejects a payload that
    does not match the kind), and append it to the operator-input store.

    Args:
        ctx: Daemon context (needs ``state_path``).
        campaign_id: The target campaign id (resolved against the store).
        kind: The :class:`OperatorInputKind` this input carries.
        scope: The blackboard scope the input addresses.
        note: The operator's free-text WHY / the input text.
        urgency: The input's urgency rung.
        payload: The kind-specific payload (``None`` for a broadcast).

    Returns:
        Dict matching :class:`OperatorInputResult`.

    Raises:
        RuntimeError: When ``ctx.state_path`` is unset.
        ValueError: When the campaign is missing / not active.
    """
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    resolved = _resolve_active_campaign_id(state_path, campaign_id)
    op_input = OperatorInput(
        campaign_id=resolved,
        kind=kind,
        scope=scope,
        note=note,
        urgency=urgency,
        payload=payload,
        at=datetime.now(UTC),
    )
    input_id = persist_operator_input(state_path, op_input)
    logger.info(
        f"_append_operator_input campaign={resolved!r} kind={kind.value} "
        f"blocking={op_input.is_blocking} id={input_id!r}"
    )
    return OperatorInputResult(
        campaign_id=resolved,
        kind=kind.value,
        input_id=input_id,
        blocking=op_input.is_blocking,
    ).model_dump(mode="json")


@register("research.steer")
async def steer(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Push an operator steer note onto a running campaign's channel.

    The honest steer surface the board's ``t`` key routes to: it appends a
    typed ``steer`` :class:`OperatorInput` to the daemon-owned append-log so the
    next round's fold sees it (a steer is between-rounds feedback, not a
    blocking interrupt). The board's ``t`` key now lands a real row.

    Args:
        ctx: Server context -- needs ``state_path``.
        params: JSON-RPC params per :class:`SteerParams`.

    Returns:
        Dict matching :class:`OperatorInputResult`.

    Raises:
        ValueError: When *params* does not validate or the campaign is
            missing / not active. Mapped to ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = SteerParams.model_validate(params)
    return _append_operator_input(
        ctx,
        campaign_id=args.campaign_id,
        kind=OperatorInputKind.STEER,
        scope=args.scope,
        note=args.text,
        urgency=args.urgency,
        payload=SteerPayload(action=args.action),
    )


@register("research.broadcast")
async def broadcast(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Fan an operator notice to every running round of a campaign.

    The honest broadcast surface the board's ``b`` key routes to: it appends a
    typed ``notice-broadcast`` :class:`OperatorInput` (no payload -- the note
    carries the broadcast) to the daemon-owned append-log so the orchestrator
    distributes it on the next task. The board's ``b`` key now lands a real row.

    Args:
        ctx: Server context -- needs ``state_path``.
        params: JSON-RPC params per :class:`BroadcastParams`.

    Returns:
        Dict matching :class:`OperatorInputResult`.

    Raises:
        ValueError: When *params* does not validate or the campaign is
            missing / not active.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = BroadcastParams.model_validate(params)
    return _append_operator_input(
        ctx,
        campaign_id=args.campaign_id,
        kind=OperatorInputKind.NOTICE_BROADCAST,
        scope=args.scope,
        note=args.notice,
        urgency=args.urgency,
        payload=None,
    )


@register("research.override")
async def override(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Force an operator verdict onto a campaign's blocking fork.

    The honest override surface the board's ``v`` key routes to: it appends a
    typed ``override`` :class:`OperatorInput` carrying an
    :class:`~eawf.kernel.spec.operator_input.OverridePayload` to the
    daemon-owned append-log. A locked override persists across rounds (D-3)
    until a later override on the same scope clears the lock; it lands at
    ``URGENT`` by default so it soft-pauses the round until folded (D-2). The
    board's ``v`` key now lands a real row.

    Args:
        ctx: Server context -- needs ``state_path``.
        params: JSON-RPC params per :class:`OverrideParams`.

    Returns:
        Dict matching :class:`OperatorInputResult`.

    Raises:
        ValueError: When *params* does not validate or the campaign is
            missing / not active.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    args = OverrideParams.model_validate(params)
    return _append_operator_input(
        ctx,
        campaign_id=args.campaign_id,
        kind=OperatorInputKind.OVERRIDE,
        scope=args.scope,
        note=args.verdict,
        urgency=args.urgency,
        payload=OverridePayload(value=args.verdict, locked=args.locked),
    )


__all__ = [
    "AddQuestionParams",
    "AddQuestionResult",
    "AgentEndProducer",
    "BroadcastParams",
    "CancelCampaignParams",
    "CancelCampaignResult",
    "CreateCampaignParams",
    "CreateCampaignResult",
    "FollowupParams",
    "FollowupResult",
    "OperatorInputResult",
    "OverrideParams",
    "ResearchRoundPayload",
    "RunCampaignParams",
    "RunCampaignResult",
    "SnapshotParams",
    "SnapshotResult",
    "StageCampaignParams",
    "StageCampaignResult",
    "SteerParams",
    "add_question",
    "broadcast",
    "build_bound_round_runner",
    "build_live_dispatch_spawner",
    "cancel_campaign",
    "create_campaign",
    "fold_operator_channel",
    "followup",
    "override",
    "persist_campaign",
    "persist_operator_input",
    "read_campaign_rounds",
    "read_latest_campaign",
    "read_operator_inputs",
    "reconcile_round_claims",
    "run",
    "run_campaign",
    "snapshot",
    "stage_campaign_method",
    "steer",
]
