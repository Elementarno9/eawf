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
from eawf.kernel.state.enums import CampaignStatus, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import (
    CampaignTombstone,
    ResearchCampaignPayload,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import MethodContext, register

if TYPE_CHECKING:
    from eawf.kernel.spec.research_campaign import StagedDispatch
    from eawf.kernel.spec.round_loop import RoundOutcome

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


__all__ = [
    "AgentEndProducer",
    "CancelCampaignParams",
    "CancelCampaignResult",
    "CreateCampaignParams",
    "CreateCampaignResult",
    "StageCampaignParams",
    "StageCampaignResult",
    "build_bound_round_runner",
    "build_live_dispatch_spawner",
    "cancel_campaign",
    "create_campaign",
    "persist_campaign",
    "read_latest_campaign",
    "stage_campaign_method",
]
