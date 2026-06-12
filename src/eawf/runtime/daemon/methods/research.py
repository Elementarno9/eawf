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

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.campaign_driver import (
    DispatchSpawner,
    RoundSaturationReducer,
    build_round_runner,
)
from eawf.kernel.spec.research_campaign import (
    ResearchProfileBlock,
    StagedCampaign,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    CampaignStatus,
    ClaimStatus,
    OpenQuestionStatus,
    StoreKind,
    Urgency,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import (
    CampaignTombstone,
    ResearchCampaignPayload,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import MethodContext, register

if TYPE_CHECKING:
    from eawf.kernel.spec.campaign_driver import RoundFindings
    from eawf.kernel.spec.research_campaign import StagedDispatch
    from eawf.kernel.spec.round_loop import RoundOutcome
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: Type of the per-dispatch agent-end producer the live dispatch spawner
#: wraps. Production binds a closure that drives the daemon ``agent.dispatch``
#: spawn=True path for a researcher dispatch and returns the spawned agent's
#: decoded ``agent_end`` body; a test binds a stub returning a fixture body.
#: Keeping the producer injected means the binding (StagedDispatch ->
#: agent.dispatch -> agent_end parse) is exercised under a stubbed spawner +
#: fixture bodies without spawning a real subprocess.
AgentEndProducer = Callable[["StagedDispatch"], Mapping[str, object]]


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


__all__ = [
    "AddQuestionParams",
    "AddQuestionResult",
    "AgentEndProducer",
    "CancelCampaignParams",
    "CancelCampaignResult",
    "CreateCampaignParams",
    "CreateCampaignResult",
    "StageCampaignParams",
    "StageCampaignResult",
    "add_question",
    "build_bound_round_runner",
    "build_live_dispatch_spawner",
    "cancel_campaign",
    "create_campaign",
    "persist_campaign",
    "read_latest_campaign",
    "reconcile_round_claims",
    "stage_campaign_method",
]
