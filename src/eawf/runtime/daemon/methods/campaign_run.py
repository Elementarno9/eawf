"""Starting and driving a research Campaign from the daemon.

``runtime.campaign.start`` plans a Campaign from a research brief -- the
questions, the depth each is worked to, the budget -- approves the plan and,
unless asked not to, starts driving it. ``runtime.campaign.run`` drives a
Campaign already approved, such as one a drive paused or one approved by
hand. A drive runs in the daemon behind the answer: the verb answers once
the drive is underway, and the Campaign's own record shows its steps,
artifacts and findings as each round lands.

A brief that leaves the depth or the fan-out width unset takes the
``research.default_depth`` and ``research.agent_count`` layers of the
repository. The research agent is the configured runtime's headless session,
asked for one round's report in a closed JSON shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.layered import get_dotted, merge_config, resolve_runtime_tier_models
from eawf.kernel.spec.research import ResearchDepth, resolve_default_research_depth
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import NonEmptyStr, PrincipalKey
from eawf.kernel.state.epoch2.campaign import ResearchBudget, StepTitle
from eawf.kernel.state.epoch2.urns import TrackUrn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.campaign_scheduler import (
    CampaignDrive,
    ResearchAgent,
    StepAssignment,
    StepReport,
    drive_campaign,
    plan_campaign,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.campaign import approve_plan
from eawf.runtime.daemon.methods.delivery_approval import publish_commits
from eawf.runtime.daemon.methods.host_question import question_keys
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.runtimes.metering import price_spawn_result
from eawf.runtime.runtimes.selector import select_adapter
from eawf.workflow.dispatch.routing import model_for_runtime

logger = logging.getLogger(__name__)

CAMPAIGN_START_METHOD: Final = "runtime.campaign.start"
CAMPAIGN_RUN_METHOD: Final = "runtime.campaign.run"

#: The fan-out width a repository that sets no ``research.agent_count`` layer gets.
DEFAULT_AGENT_COUNT: Final = 4

#: The fan-out width's band, the ``research.agent_count`` leaf's own.
AGENT_COUNT_BAND: Final = (1, 12)

#: Each runtime's spelling on the routing table.
_ROUTING_RUNTIMES: Final = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}

#: The effort a round is routed at, by the depth its Campaign was planned to.
_DEPTH_EFFORT: Final = {
    ResearchDepth.SHALLOW: EffortBucket.S,
    ResearchDepth.MEDIUM: EffortBucket.M,
    ResearchDepth.DEEP: EffortBucket.L,
    ResearchDepth.EXHAUSTIVE: EffortBucket.XL,
}

_JSON_BLOCK: Final = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

#: The drives running in this daemon, by Campaign key.
_DRIVES: dict[str, asyncio.Task[CampaignDrive]] = {}

Runtime = Annotated[str, Field(pattern=r"^(claude-code|codex|opencode)$")]
AgentCount = Annotated[int, Field(ge=AGENT_COUNT_BAND[0], le=AGENT_COUNT_BAND[1], strict=True)]


class CampaignStartParams(BaseModel):
    """Params of :data:`CAMPAIGN_START_METHOD`.

    Attributes:
        actor: Who approved the brief.
        track_ref: The Track that owns the Campaign.
        title: What the Campaign researches.
        questions: The brief's questions, the Campaign's own first.
        depth: How deep each question is worked; ``None`` takes the
            ``research.default_depth`` layer.
        agents: How many ready steps a round dispatches; ``None`` takes the
            ``research.agent_count`` layer.
        budget: The Campaign's axes; ``None`` bounds it by one round per step.
        runtime: The runtime whose headless session works each round.
        drive: Whether to start driving the Campaign once it is approved.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    track_ref: TrackUrn
    title: StepTitle
    questions: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1, max_length=12)]
    depth: ResearchDepth | None = None
    agents: AgentCount | None = None
    budget: ResearchBudget | None = None
    runtime: Runtime = "claude-code"
    drive: bool = True


class CampaignRunParams(BaseModel):
    """Params of :data:`CAMPAIGN_RUN_METHOD`.

    Attributes:
        actor: Who asked for the drive.
        campaign_key: The Campaign to drive.
        agents: How many ready steps a round dispatches; ``None`` takes the
            ``research.agent_count`` layer.
        runtime: The runtime whose headless session works each round.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]
    agents: AgentCount | None = None
    runtime: Runtime = "claude-code"


class HostResearchAgent:
    """Works a round in the runtime's headless session, one session per round.

    Args:
        runtime: The runtime to spawn.
        repo_root: The repository the session reads.
        effort: The effort the round is routed at.
    """

    def __init__(self, runtime: str, repo_root: Path, effort: EffortBucket) -> None:
        self._runtime = runtime
        self._repo = repo_root
        self._effort = effort

    async def work(self, assignment: StepAssignment) -> StepReport:
        """Spawn the round's session and return its report.

        Raises:
            ValueError: The session answered no report in the asked shape.
        """
        model = model_for_runtime(
            AgentSessionRole.RESEARCHER,
            self._effort,
            _ROUTING_RUNTIMES[self._runtime],
            runtime_models=resolve_runtime_tier_models(self._repo),
        )
        result = await select_adapter(self._runtime).spawn_session(
            round_prompt(assignment), model=model, cwd=str(self._repo)
        )
        metered = price_spawn_result(result)
        return parse_round_report(result.text, tokens=metered.input_tokens + metered.output_tokens)


def round_prompt(assignment: StepAssignment) -> str:
    """Return the prompt one round of *assignment* is worked from."""
    prior = "\n".join(f"- {outcome}" for outcome in assignment.prior_outcomes) or "- none yet"
    return (
        f"You are the researcher for step {assignment.ordinal} of the research Campaign "
        f"{assignment.campaign_title!r}.\n\n"
        f"Step: {assignment.title}\nMethod: {assignment.method.value}\n"
        f"Question: {assignment.question}\n\nWhat earlier steps showed:\n{prior}\n\n"
        "Work the question read-only; change no file. Back every claim with a file:line, "
        "a store URN or a URL, and mark a claim you cannot back as unresolved.\n\n"
        "End with one fenced ```json block holding exactly these keys: "
        '"report" (the round\'s report as markdown), "outcome" (what the step showed, one '
        'line), "sources" (how many sources you consulted) and "findings" (what should '
        "outlive the Campaign, one line each; empty unless this step settles something)."
    )


def parse_round_report(text: str, *, tokens: int) -> StepReport:
    """Return the report a round's session answered with.

    Args:
        text: The session's final text.
        tokens: The tokens the session consumed, as metered.

    Returns:
        The validated report.

    Raises:
        ValueError: No JSON block, or one that does not hold the report shape.
    """
    blocks = _JSON_BLOCK.findall(text)
    if not blocks:
        raise ValueError("the round answered no ```json report block")
    body = json.loads(blocks[-1])
    if not isinstance(body, dict):
        raise ValueError("the round's report block is not an object")
    return StepReport.model_validate({**body, "tokens": tokens})


def research_agent_for(runtime: str, repo_root: Path, depth: ResearchDepth) -> ResearchAgent:
    """Return the agent that works a Campaign's rounds on *runtime*."""
    return HostResearchAgent(runtime, repo_root, _DEPTH_EFFORT[depth])


def _layers(repo_root: Path) -> dict[str, Any]:
    merged, _sources = merge_config(repo=repo_root, workspace=repo_root)
    return merged


def resolve_agent_count(merged: dict[str, Any], explicit: int | None) -> int:
    """Return the fan-out width: *explicit*, else the ``research.agent_count`` layer.

    A layer outside the leaf's band is clamped into it.
    """
    if explicit is not None:
        return explicit
    try:
        raw = get_dotted(merged, "research.agent_count")
    except KeyError:
        return DEFAULT_AGENT_COUNT
    low, high = AGENT_COUNT_BAND
    return min(max(raw, low), high) if isinstance(raw, int) else DEFAULT_AGENT_COUNT


def campaign_drive_in_flight(campaign_key: str | None = None) -> bool:
    """Return whether this daemon is driving *campaign_key*, or any Campaign."""
    live = {key for key, task in _DRIVES.items() if not task.done()}
    return bool(live) if campaign_key is None else campaign_key in live


def _begin(
    ctx: MethodContext,
    context: Epoch2RootContext,
    *,
    campaign_key: str,
    agent: ResearchAgent,
    actor: str,
    width: int,
) -> bool:
    """Start driving *campaign_key* behind the answer; ``False`` when a drive already runs."""
    if campaign_drive_in_flight(campaign_key):
        return False

    def publish(envelopes: tuple[Envelope, ...]) -> None:
        publish_commits(ctx, envelopes)

    task = asyncio.create_task(
        drive_campaign(
            context,
            campaign_key=campaign_key,
            agent=agent,
            actor=actor,
            width=width,
            publish=publish,
        )
    )

    def finished(done: asyncio.Task[CampaignDrive]) -> None:
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error(f"campaign drive failed campaign={campaign_key} error={error!r}")
            return
        drive = done.result()
        logger.info(f"campaign drive ended campaign={campaign_key} disposition={drive.disposition}")

    task.add_done_callback(finished)
    _DRIVES[campaign_key] = task
    return True


@native_mutator(CAMPAIGN_START_METHOD)
async def start_campaign(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Plan a Campaign from its brief, approve it and start driving it.

    Returns:
        ``{"record": <Campaign>, "committed": bool, "driving": bool}``.

    Raises:
        DaemonValidationError: The request does not parse, or a layer the
            brief leaves unset holds a value outside its ladder.
        TransactionRefusedError: The plan approval was refused.
    """
    args = native_params(CampaignStartParams, params)
    context = ctx.native_root_context(authority.root)
    repo_root = authority.root.parent
    merged = await asyncio.to_thread(_layers, repo_root)
    try:
        depth = args.depth or resolve_default_research_depth(merged)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    path = document_path(context.require_selected_generation())
    records = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    plan = plan_campaign(
        question_keys(read_document(path), records),
        actor=args.actor,
        track_ref=args.track_ref,
        title=args.title,
        questions=args.questions,
        depth=depth,
        budget=args.budget,
    )
    commit, envelopes = await asyncio.to_thread(approve_plan, context, plan, now=datetime.now(UTC))
    publish_commits(ctx, envelopes)
    driving = args.drive and _begin(
        ctx,
        context,
        campaign_key=str(commit.record["key"]),
        agent=research_agent_for(args.runtime, repo_root, depth),
        actor=args.actor,
        width=resolve_agent_count(merged, args.agents),
    )
    logger.info(f"start_campaign campaign={commit.record['key']} depth={depth} driving={driving}")
    return {**commit.model_dump(mode="json"), "driving": driving}


@native_mutator(CAMPAIGN_RUN_METHOD)
async def run_campaign(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Start driving an approved Campaign.

    Returns:
        ``{"campaign_key": str, "driving": bool}``; ``driving`` is ``False``
        when this daemon already drives it.

    Raises:
        DaemonValidationError: The request does not parse, or the tree holds
            no such Campaign.
    """
    args = native_params(CampaignRunParams, params)
    context = ctx.native_root_context(authority.root)
    repo_root = authority.root.parent
    document = read_document(document_path(context.require_selected_generation()))
    if args.campaign_key not in document_rows(document, Epoch2Collection.CAMPAIGN):
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree holds no campaign keyed "
            f"{args.campaign_key!r}"
        )
    merged = await asyncio.to_thread(_layers, repo_root)
    depth = resolve_default_research_depth(merged)
    driving = _begin(
        ctx,
        context,
        campaign_key=args.campaign_key,
        agent=research_agent_for(args.runtime, repo_root, depth),
        actor=args.actor,
        width=resolve_agent_count(merged, args.agents),
    )
    logger.info(f"run_campaign campaign={args.campaign_key} driving={driving}")
    return {"campaign_key": args.campaign_key, "driving": driving}


__all__ = [
    "CAMPAIGN_RUN_METHOD",
    "CAMPAIGN_START_METHOD",
    "CampaignRunParams",
    "CampaignStartParams",
    "HostResearchAgent",
    "campaign_drive_in_flight",
    "parse_round_report",
    "research_agent_for",
    "resolve_agent_count",
    "round_prompt",
]
