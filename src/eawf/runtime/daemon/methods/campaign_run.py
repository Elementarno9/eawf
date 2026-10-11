"""Starting and driving a research Campaign from the daemon.

``runtime.campaign.start`` plans a Campaign from a research brief -- the
questions, the depth each is worked to, the budget -- approves the plan and,
unless asked not to, starts driving it. ``runtime.campaign.run`` drives a
Campaign already approved, such as one a drive paused or one approved by
hand. A drive runs in the daemon behind the answer: the verb answers once
the drive is underway, and the Campaign's own record shows its steps,
artifacts and findings as each round lands.

A question may carry its own context and a brief file. The brief is read
once, when the Campaign is planned, and kept on the Campaign with its digest,
so every round of the question is worked from the same text. A start names
its request with an idempotency key, and a retry under it is answered with
the Campaign the first one started; a drive names the Campaign revision it
was decided at, and a Campaign that moved since is refused.

A brief that leaves the depth or the fan-out width unset takes the
``research.default_depth`` and ``research.agent_count`` layers of the
repository. The research agent runs each round as a read-only Run through the
configured runtime's official provider driver, and asks it for one round's
report in a closed JSON shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import eawf.runtime.worktree.git as git
from eawf.kernel.config.layered import get_dotted, merge_config, resolve_runtime_tier_models
from eawf.kernel.config.providers import parse_provider_configuration
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule, StopCondition
from eawf.kernel.runtime.compiled import (
    CompiledRunSpec,
    ModelNarrowing,
    PolicyOverlay,
    RunCompileRequest,
    canonical_digest,
)
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.provider import UniqueToolIds
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.spec.research import ResearchDepth, resolve_default_research_depth
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import NonEmptyStr, PrincipalKey
from eawf.kernel.state.epoch2.campaign import (
    BRIEF_MAX_CHARS,
    Campaign,
    CampaignQuestionSpec,
    QuestionBrief,
    ResearchBudget,
    StepTitle,
)
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import CampaignScope, RunPurpose
from eawf.kernel.state.epoch2.urns import TrackUrn
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.campaign_scheduler import (
    CampaignDrive,
    PlannedQuestion,
    ResearchAgent,
    StepAssignment,
    StepReport,
    UnreportedRoundError,
    drive_campaign,
    failure_detail,
    plan_campaign,
    planned_depth,
)
from eawf.runtime.daemon.dispatch_stream import usage_sink
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.campaign import MAX_TEXT_BYTES, approve_plan
from eawf.runtime.daemon.methods.delivery_approval import publish_commits
from eawf.runtime.daemon.methods.host_question import question_keys
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_dispatch import (
    NATIVE_LAUNCHERS,
    CapsuleRequest,
    bind_contract,
    run_ledger,
    seal_capsule,
)
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.provider_facts import installed_provider_facts
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
    TurnOutcome,
)
from eawf.runtime.runtimes.metering import UsageSample
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.dispatch.routing import model_for_runtime
from eawf.workflow.runtime.compile import compile_run_spec

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

#: The longest step outcome and finding line a report holds, as the prompt states them.
_OUTCOME_MAX_CHARS: Final = 500
_FINDING_MAX_CHARS: Final = 200

#: What ends a finding shortened to fit its line.
_CLIP_MARK: Final = "…"

_JSON_BLOCK: Final = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

#: Each runtime a shipped driver runs, by the provider id its driver manifest names.
_LAUNCHED_PROVIDERS: Final = {"claude-code": "claude", "codex": "codex"}

#: Where the effort-routed model a round prefers says it came from.
_ROUND_MODEL_SOURCE: Final = "policy://campaign-round/effort-model"

#: The schema a round's report answers in.
_ROUND_REPORT_SCHEMA: Final = "schema://researcher-report/v1"

#: The semantic tools a round may ask for: reads and its own terminal report, so its
#: capsule grants no write.
_ROUND_TOOLS: Final[UniqueToolIds] = (
    SemanticToolId.REPO_READ.value,
    SemanticToolId.REPO_SEARCH.value,
    SemanticToolId.SUBMIT_REPORT.value,
)

#: The drives running in this daemon, by tree root id and Campaign key: one daemon
#: serves several trees, and each numbers its Campaigns from one.
_DRIVES: dict[tuple[str, str], asyncio.Task[CampaignDrive]] = {}

Runtime = Annotated[str, Field(pattern=r"^(claude-code|codex|opencode)$")]
AgentCount = Annotated[int, Field(ge=AGENT_COUNT_BAND[0], le=AGENT_COUNT_BAND[1], strict=True)]


class CampaignStartParams(BaseModel):
    """Params of :data:`CAMPAIGN_START_METHOD`.

    Attributes:
        actor: Who approved the brief.
        track_ref: The Track that owns the Campaign.
        title: What the Campaign researches.
        questions: The brief's questions, the Campaign's own first; a bare
            string is a question with no context and no brief.
        depth: How deep each question is worked; ``None`` takes the
            ``research.default_depth`` layer.
        agents: How many ready steps a round dispatches; ``None`` takes the
            ``research.agent_count`` layer.
        budget: The Campaign's axes; ``None`` bounds it by one round per step.
        runtime: The runtime whose provider driver works each round.
        drive: Whether to start driving the Campaign once it is approved.
        idempotency_key: The caller's name for the request; a retry under it
            is answered with the Campaign the first one started. ``None``
            names none, and the Campaign is matched on its plan instead.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    track_ref: TrackUrn
    title: StepTitle
    questions: Annotated[
        tuple[NonEmptyStr | CampaignQuestionSpec, ...], Field(min_length=1, max_length=12)
    ]
    depth: ResearchDepth | None = None
    agents: AgentCount | None = None
    budget: ResearchBudget | None = None
    runtime: Runtime = "claude-code"
    drive: bool = True
    idempotency_key: IdempotencyKey | None = None


class CampaignRunParams(BaseModel):
    """Params of :data:`CAMPAIGN_RUN_METHOD`.

    Attributes:
        actor: Who asked for the drive.
        campaign_key: The Campaign to drive.
        agents: How many ready steps a round dispatches; ``None`` takes the
            ``research.agent_count`` layer.
        runtime: The runtime whose provider driver works each round.
        expected_revision: The Campaign revision the drive was decided at; a
            Campaign that moved since is refused. ``None`` drives whatever
            revision stands.
    """

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]
    agents: AgentCount | None = None
    runtime: Runtime = "claude-code"
    expected_revision: Annotated[int, Field(gt=0, strict=True)] | None = None


class HostResearchAgent:
    """Works each round through the runtime's official provider driver, one Run per round.

    A round is compiled like any dispatched Run -- the installed drivers, their
    machine certifications and the provider layers pick the profile and pin the
    model -- under the read-only research purpose, so the compiler's safe
    baseline leaves it a read-only sandbox and no workspace write. It runs in a
    detached checkout of ``HEAD`` outside the repository, which the driver's
    per-Run files land in and which is removed once the turn ends.

    The round's Run records the contract binding of the capsule its worker is
    handed before the worker starts, because the semantic gateway admits a call
    only under the capsule the Run is bound to.

    Each usage reading the driver hands back is stated as ``usage_observed`` on the
    round's Run, as a dispatched Run's are, so the Run's own stream carries what the
    round spent; the last reading is also what the round's report is charged.

    Args:
        runtime: The runtime to drive.
        context: The native context of the tree the Campaign belongs to.
        effort: The effort the round is routed at.
        launchers: The driver each provider kind is started through.
    """

    def __init__(
        self,
        runtime: str,
        context: Epoch2RootContext,
        effort: EffortBucket,
        *,
        launchers: Mapping[str, NativeRunLauncher] = NATIVE_LAUNCHERS,
    ) -> None:
        self._runtime = runtime
        self._context = context
        self._tree = context.identity.tree_root
        self._effort = effort
        self._launchers = launchers

    async def work(self, assignment: StepAssignment) -> StepReport:
        """Run the round's turn through the driver and return its report.

        Raises:
            ValueError: No shipped driver serves the runtime, or the round
                compiles to no spec.
            UnreportedRoundError: The turn ended with no report in the asked
                shape; it carries the tokens the turn was metered for.
            RuntimeSpawnError: The driver could not start the provider.
        """
        spec, capsule = await asyncio.to_thread(self._compile, assignment)
        launcher = self._launchers.get(spec.provider_options.provider_kind)
        if launcher is None:
            raise ValueError(f"no launcher serves provider {spec.provider_options.provider_kind!r}")
        await asyncio.to_thread(self._bind, spec, capsule)
        readings: list[UsageSample] = []
        stated = usage_sink(
            self._context,
            parse_qualified_urn(str(assignment.run_ref)),
            actor=assignment.actor,
            attempt_ref=str(assignment.run_ref),
            meter=None,
        )

        async def metered(sample: UsageSample, pgid: int | None) -> bool:
            readings.append(sample)
            return await stated(sample, pgid)

        async with _round_checkout(self._tree.parent) as workspace:
            launched = await launcher.launch(
                NativeLaunchRequest(
                    spec=spec,
                    capsule=capsule,
                    workspace_handle=f"wsh-{secrets.token_hex(16)}",
                    workspace=workspace,
                    repo_root=self._tree.parent,
                    prompt=round_prompt(assignment),
                    hello_sequence=1,
                    usage_sink=metered,
                )
            )
            with supervisor_of(self._context).worker(str(spec.run_ref), launched.subprocess_pid):
                ended = await _turn_of(launched)
        last = readings[-1] if readings else None
        tokens = None if last is None else last.input_tokens + last.output_tokens
        if ended.exit_kind != "completed" or ended.answer_text is None:
            raise UnreportedRoundError(
                f"the round's turn ended {ended.exit_kind} with no answer: {ended.error_text}",
                tokens=tokens,
            )
        try:
            report = parse_round_report(ended.answer_text, tokens=tokens)
        except ValueError as error:
            raise UnreportedRoundError(str(error), tokens=tokens) from error
        session = VendorSessionRef(
            harness=self._runtime, session_digest=launched.provider_session_ref
        )
        runtime = ended.runtime_tuple or launched.runtime_tuple
        return report.model_copy(update={"runtime": runtime, "session": session})

    def _bind(self, spec: CompiledRunSpec, capsule: AuthorityCapsule) -> None:
        """Record the round's Run as bound to *capsule*, the one its worker holds."""
        with self._context.session([spec.run_ref]) as session:
            bind_contract(
                session,
                binding=RunBinding(
                    run_ref=spec.run_ref,
                    compiled_spec_digest=spec.contract_digest,
                    authority_capsule_digest=capsule.contract_digest,
                    route_policy_revision=spec.route_policy_revision,
                    bound_at=datetime.now(UTC),
                    capsule=capsule,
                ),
                records=read_ledger_records(run_ledger(session)),
            )

    def _compile(self, assignment: StepAssignment) -> tuple[CompiledRunSpec, AuthorityCapsule]:
        """Compile the round's read-only spec and seal its capsule.

        The routing table's model for the round's effort rides as the Run's
        preferred default; a model the profile does not allow leaves the
        profile's own default standing.

        Raises:
            ValueError: No shipped driver runs the runtime, no certified
                binding serves its provider, or a compile rule refuses the
                round.
            ProviderConfigError: A provider layer does not load.
        """
        provider = _LAUNCHED_PROVIDERS.get(self._runtime)
        if provider is None:
            raise ValueError(f"no shipped driver runs {self._runtime!r}")
        now = datetime.now(UTC)
        facts = installed_provider_facts(self._tree, now=now)
        bindings = tuple(b for b in facts.bindings if b.manifest.provider_id == provider)
        if not bindings:
            raise ValueError(f"no certified driver serves provider {provider!r}")
        configuration = parse_provider_configuration(
            {
                "global": facts.config["global"]["runtime"],
                "repository": facts.config["repository"]["runtime"],
            },
            registry=facts.registry,
        )
        model = model_for_runtime(
            AgentSessionRole.RESEARCHER,
            self._effort,
            _ROUTING_RUNTIMES[self._runtime],
            runtime_models=resolve_runtime_tier_models(self._tree.parent),
        )
        spec = compile_run_spec(
            RunCompileRequest(
                run_ref=assignment.run_ref,
                run_scope=CampaignScope(
                    scope_kind="campaign",
                    purpose=RunPurpose.RESEARCH,
                    campaign_ref=assignment.campaign_ref,
                ),
                purpose=RunPurpose.RESEARCH,
                agent_role=AgentSessionRole.RESEARCHER,
            ),
            configuration=configuration,
            bindings=bindings,
            compiled_at=now,
            policy_overlays=(
                PolicyOverlay(
                    layer="run_override",
                    source_ref=_ROUND_MODEL_SOURCE,
                    model_policy=ModelNarrowing(default=model),
                ),
            ),
        )
        capsule = seal_capsule(
            spec=spec,
            request=CapsuleRequest(
                criteria_digest=canonical_digest(
                    assignment.model_dump(mode="json", include={"campaign_ref", "ordinal"})
                ),
                report_schema_ref=_ROUND_REPORT_SCHEMA,
                tool_grants=_ROUND_TOOLS,
                stop_conditions=tuple(StopCondition),
            ),
        )
        return spec, capsule


@contextlib.asynccontextmanager
async def _round_checkout(repo_root: Path) -> AsyncIterator[Path]:
    """Yield a detached checkout of *repo_root*'s ``HEAD`` outside the repository.

    The round reads the repository's files and history from it, and the
    driver writes its per-Run files into it, never into *repo_root*.

    Raises:
        UserError: *repo_root* has no ``HEAD`` commit, or git is absent.
        StateConflict: ``git worktree add`` failed.
    """
    scratch = Path(tempfile.mkdtemp(prefix="eawf-round-"))
    checkout = scratch / "checkout"
    try:
        await asyncio.to_thread(
            git.worktree_add, repo_root, branch=None, path=checkout, base="HEAD"
        )
        yield checkout
    finally:
        if checkout.exists():
            try:
                await asyncio.to_thread(git.worktree_remove, repo_root, path=checkout, force=True)
            except cli_errors.StateConflict as error:
                # git prunes the registration of a checkout whose directory is gone
                logger.warning(f"round checkout not removed by git error={error}")
        shutil.rmtree(scratch, ignore_errors=True)


async def _turn_of(launched: NativeLaunchOutcome) -> TurnOutcome:
    """Await the turn *launched* started, stopping it if the round is cancelled.

    Raises:
        ValueError: The driver handed back no turn to read the answer from.
    """
    turn = launched.turn
    if turn is None:
        raise ValueError("the driver handed back no turn to read the round's answer from")
    try:
        return await turn
    finally:
        if not turn.done():
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn


def round_prompt(assignment: StepAssignment) -> str:
    """Return the prompt one round of *assignment* is worked from.

    The question's context and brief follow the question, each under its own
    label, and come before what earlier steps showed; a question that states
    neither gets neither section.
    """
    prior = "\n".join(f"- {outcome}" for outcome in assignment.prior_outcomes) or "- none yet"
    framing = ""
    if assignment.context:
        framing += f"Context:\n{assignment.context}\n\n"
    if assignment.brief is not None:
        framing += f"Brief ({assignment.brief.path}):\n{assignment.brief.text}\n\n"
    return (
        f"You are the researcher for step {assignment.ordinal} of the research Campaign "
        f"{assignment.campaign_title!r}.\n\n"
        f"Step: {assignment.title}\nMethod: {assignment.method.value}\n"
        f"Question: {assignment.question}\n\n{framing}What earlier steps showed:\n{prior}\n\n"
        "Work the question read-only; change no file. Back every claim with a file:line, "
        "a store URN or a URL, and mark a claim you cannot back as unresolved.\n\n"
        "End with one fenced ```json block holding exactly these keys and no other: "
        '"report" (a string: the round\'s report as markdown, at most '
        f'{MAX_TEXT_BYTES} characters), "outcome" (a string: what '
        f"the step showed, one line of at most {_OUTCOME_MAX_CHARS} characters), "
        '"sources" (a whole number: how many sources you consulted, not a list of them) and '
        '"findings" (a list of strings: what should outlive the Campaign, each one line of '
        f"at most {_FINDING_MAX_CHARS} characters, a longer one being cut short; an empty "
        "list unless this step settles something)."
    )


def parse_round_report(text: str, *, tokens: int | None) -> StepReport:
    """Return the report a round's session answered with.

    Args:
        text: The session's final text.
        tokens: The tokens the session consumed, as metered; ``None`` when no reading
            metered them.

    Returns:
        The validated report.

    A ``sources`` given as the list of sources consulted is taken as its count,
    since the count is all the report keeps of it. An outcome or a one-line
    finding past its bound is clipped to fit and marked so, and the report
    states it in full, because the round already paid for what it learned. A
    report past its own bound is refused, since nothing else could keep it in
    full, as is any other mismatch.

    Raises:
        ValueError: No JSON block, one that is not a JSON object, or one that
            does not hold the report shape; the message names each refused
            field and why, never the round's text.
    """
    blocks = _JSON_BLOCK.findall(text)
    if not blocks:
        raise ValueError("the round answered no ```json report block")
    try:
        body = json.loads(blocks[-1])
    except json.JSONDecodeError as error:
        raise ValueError(f"the round's report block is not valid JSON: {error}") from error
    if not isinstance(body, dict):
        raise ValueError("the round's report block is not an object")
    if isinstance(body.get("sources"), list):
        body["sources"] = len(body["sources"])
    in_full: list[str] = []
    outcome = body.get("outcome")
    outcome_clipped = isinstance(outcome, str) and len(outcome.strip()) > _OUTCOME_MAX_CHARS
    if isinstance(outcome, str) and outcome_clipped:
        in_full.append(f"## Outcome in full\n\n{outcome.strip()}")
        body["outcome"] = _clip(outcome.strip(), _OUTCOME_MAX_CHARS)
    overlong: list[str] = []
    if isinstance(body.get("findings"), list):
        overlong = [f.strip() for f in body["findings"] if _clippable(f)]
        body["findings"] = [
            _clip(f.strip(), _FINDING_MAX_CHARS) if _clippable(f) else f for f in body["findings"]
        ]
    if overlong:
        stated = "\n".join(f"- {finding}" for finding in overlong)
        in_full.append(f"## Findings in full\n\n{stated}")
    if in_full and isinstance(body.get("report"), str):
        body["report"] += "".join(f"\n\n{section}" for section in in_full) + "\n"
    try:
        return StepReport.model_validate(
            {
                **body,
                "tokens": tokens,
                "outcome_clipped": outcome_clipped,
                "clipped_findings": tuple(_clip(f, _FINDING_MAX_CHARS) for f in overlong),
            }
        )
    except ValidationError as error:
        raise ValueError(
            f"the round's report does not hold the asked shape: {failure_detail(error)}"
        ) from error


def _clippable(finding: object) -> bool:
    """Return whether *finding* is one line that only its length keeps from fitting."""
    if not isinstance(finding, str):
        return False
    line = finding.strip()
    return len(line) > _FINDING_MAX_CHARS and "\n" not in line and "\r" not in line


def _clip(text: str, bound: int) -> str:
    """Return *text* cut at its last word boundary to end in the clip mark within *bound*."""
    room = bound - len(_CLIP_MARK)
    head = text[:room]
    if not text[room].isspace() and len(head.split()) > 1:
        head = head.rsplit(maxsplit=1)[0]
    return f"{head.rstrip()}{_CLIP_MARK}"


def read_brief(repo_root: Path, path: str) -> QuestionBrief:
    """Read a question's brief once, as the Campaign is planned.

    Args:
        repo_root: The repository the brief path is relative to.
        path: The brief, relative to *repo_root*.

    Returns:
        The brief's text with the digest of its bytes.

    Raises:
        DaemonValidationError: The path resolves outside the repository, names
            no readable UTF-8 file, or the brief is blank or longer than
            :data:`BRIEF_MAX_CHARS` characters.
    """
    root = repo_root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise DaemonValidationError(
            f"validation_failed: brief {path!r} names no file inside the repository"
        )
    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DaemonValidationError(f"validation_failed: brief {path!r} is not UTF-8") from error
    if not text.strip() or len(text) > BRIEF_MAX_CHARS:
        raise DaemonValidationError(
            f"validation_failed: brief {path!r} holds {len(text)} characters; a brief holds "
            f"1 to {BRIEF_MAX_CHARS}"
        )
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    return QuestionBrief(path=path, sha256=digest, text=text)


def _planned(repo_root: Path, question: str | CampaignQuestionSpec) -> str | PlannedQuestion:
    """Return *question* as the plan takes it, with its brief read."""
    if isinstance(question, str):
        return question
    brief = None if question.brief is None else read_brief(repo_root, question.brief)
    return PlannedQuestion(question=question.question, context=question.context, brief=brief)


def research_agent_for(
    runtime: str, context: Epoch2RootContext, depth: ResearchDepth
) -> ResearchAgent:
    """Return the agent that works a Campaign's rounds on *runtime* for the tree of *context*."""
    return HostResearchAgent(runtime, context, _DEPTH_EFFORT[depth])


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


def campaign_drive_in_flight(drive: tuple[str, str] | None = None) -> bool:
    """Return whether this daemon is driving *drive*, or any Campaign.

    Args:
        drive: The tree's root id and the Campaign key; ``None`` asks about any drive.
    """
    live = {key for key, task in _DRIVES.items() if not task.done()}
    return bool(live) if drive is None else drive in live


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
    drive = (context.identity.root_id, campaign_key)
    if campaign_drive_in_flight(drive):
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
        logger.info(
            f"campaign drive ended campaign={campaign_key} disposition={drive.disposition} "
            f"detail={drive.detail}"
        )

    task.add_done_callback(finished)
    _DRIVES[drive] = task
    return True


@native_mutator(CAMPAIGN_START_METHOD)
async def start_campaign(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Plan a Campaign from its brief, approve it and start driving it.

    Returns:
        ``{"record": <Campaign>, "committed": bool, "driving": bool}``.

    Raises:
        DaemonValidationError: The request does not parse, a question's brief
            cannot be read, or a layer the brief leaves unset holds a value
            outside its ladder.
        TransactionRefusedError: The plan approval was refused, or the
            idempotency key already started a Campaign of another Track or title.
    """
    args = native_params(CampaignStartParams, params)
    context = ctx.native_root_context(authority.root)
    repo_root = authority.root.parent
    merged = await asyncio.to_thread(_layers, repo_root)
    try:
        depth = args.depth or resolve_default_research_depth(merged)
    except ValueError as error:
        raise DaemonValidationError(f"validation_failed: {error}") from error
    questions = [await asyncio.to_thread(_planned, repo_root, q) for q in args.questions]
    path = document_path(context.require_selected_generation())
    records = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    plan = plan_campaign(
        question_keys(read_document(path), records),
        actor=args.actor,
        track_ref=args.track_ref,
        title=args.title,
        questions=questions,
        depth=depth,
        budget=args.budget,
        idempotency_key=args.idempotency_key,
    )
    commit, envelopes = await asyncio.to_thread(approve_plan, context, plan, now=datetime.now(UTC))
    publish_commits(ctx, envelopes)
    driving = args.drive and _begin(
        ctx,
        context,
        campaign_key=str(commit.record["key"]),
        agent=research_agent_for(args.runtime, context, depth),
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
        TransactionRefusedError: The Campaign moved past the expected revision.
    """
    args = native_params(CampaignRunParams, params)
    context = ctx.native_root_context(authority.root)
    repo_root = authority.root.parent
    document = read_document(document_path(context.require_selected_generation()))
    row = document_rows(document, Epoch2Collection.CAMPAIGN).get(args.campaign_key)
    if row is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree holds no campaign keyed "
            f"{args.campaign_key!r}"
        )
    campaign = Campaign.model_validate(row)
    if args.expected_revision is not None and campaign.revision != args.expected_revision:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=(
                f"{campaign.key} is at revision {campaign.revision}, not {args.expected_revision}"
            ),
            entity_ref=str(campaign.urn),
            remediation="Re-read the Campaign and decide again.",
        )
    merged = await asyncio.to_thread(_layers, repo_root)
    depth = planned_depth(campaign, resolve_default_research_depth(merged))
    driving = _begin(
        ctx,
        context,
        campaign_key=args.campaign_key,
        agent=research_agent_for(args.runtime, context, depth),
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
    "read_brief",
    "research_agent_for",
    "resolve_agent_count",
    "round_prompt",
]
