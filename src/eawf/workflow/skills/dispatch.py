"""``/dispatch`` skill — coordinate one Delivery Batch over the native Run verbs.

The coordinator brings a Batch's ready Tasks to a candidate by giving
each one its own Run, and keeps the Batch's frontier moving. It writes no
product code, holds no write scope and no lease, and never decides that
work is done: a Run report is not Task completion.

What the pass can read and what it cannot is the whole shape of this
skill. The Batch and Task read models bind the subject at an exact
cursor, and each Task row carries its lifecycle status beside its place
in the graph: the Tasks it depends on, the paths it claims and whether it
runs alone. The concurrency plan is derived from those facts before
anything is dispatched -- which Tasks fan out, which wait, and why --
and never from a parallelism number typed on the invocation: an invented
parallelism plan is the one failure the graph exists to prevent. The
lease scheduler enforces the same graph, so a dispatch that ignored the
plan would be refused rather than run.

The same honesty governs the dispatch arm. Opening a Run needs a compiled
run specification -- provider documents, a certified binding set, an
authority capsule and a rendered prompt -- and the daemon compiles it from
the Task itself: each Task standing ready on the first stage of the plan is
sent to the Task dispatch verb by key, with the ``--provider`` and
``--budget`` this invocation names, and the daemon claims it, creates its
Run and answers once the worker is announced. A refusal for one Task is
reported against that Task and the rest are still sent. An operator who
has compiled a request by hand presents it with ``--run-request`` beside
the one Task (``--task``) and the Run it runs under (``--run``); the pass
then reads the Run back and asks the daemon to dispatch exactly that
request instead. A ``--budget`` named on this invocation overrides the
presented request's own compiled Run token ceiling; omitting it leaves
that compiled default untouched. The retry arm is complete: resuming a Run
needs the Run reference and nothing else, so ``--resume`` reaches the
daemon. ``--dry-run`` sends nothing and stops on the frontier it would
have dispatched.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.planning.orchestration import GraphTask, derive_concurrency_plan
from eawf.workflow.skills._common import probe_skill_instruments
from eawf.workflow.skills.bodies.dispatch import (
    ConcurrencyPlan,
    DispatchBody,
    DispatchedRun,
    DispatchOutcome,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.catalog import resolve_skill
from eawf.workflow.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import (
    OutputRendering,
    RpcCaller,
    RpcRefusedError,
    RpcScope,
    daemon_rpc_caller,
    row_keys,
    status_for,
)
from eawf.workflow.skills.registry import register

logger = logging.getLogger(__name__)


#: The catalog row this skill's allowlist and grammar are read from.
_ENTRY: Final = resolve_skill("/dispatch")

#: The Batch read model the pass binds its subject at.
BATCH_READ_METHOD: Final = "projection.batch.detail.read"

#: The Task read model the candidate frontier is derived from.
TASK_READ_METHOD: Final = "projection.task.detail.read"

#: The Run read model a resumed Run is re-derived from.
RUN_READ_METHOD: Final = "projection.run.detail.read"

#: The verb that runs one Task as a new Run, compiled by the daemon.
TASK_DISPATCH_METHOD: Final = "runtime.task.dispatch"

#: The verb that opens one Run for one Task from a presented request.
RUN_DISPATCH_METHOD: Final = "runtime.run.dispatch"

#: The verb that resumes or relinks one Run.
RUN_RETRY_METHOD: Final = "runtime.run.retry"

#: Every JSON-RPC method this skill may address: the catalog row's allowlist.
#: A call outside the set is refused before the transport is touched.
RPC_SCOPE: Final = RpcScope(skill="/dispatch", methods=_ENTRY.effects.rpcs)

#: The complete accepted invocation, brackets optional and ``...`` repeatable.
INVOCATION_GRAMMAR: Final = _ENTRY.grammar.usage

#: What this skill may cause, stated as the boundary it never crosses.
EFFECTS: Final = (
    "Coordinator read models plus the Run dispatch and retry verbs. The coordinator holds no "
    "write scope and no lease, edits no repository, and records no Task completion."
)

#: The typed report every invocation produces.
OUTPUT_SCHEMA: Final = "coordination_report"

#: The closed set of ways one invocation may end.
TERMINAL_OUTCOMES: Final[tuple[DispatchOutcome, ...]] = (
    "candidate_ready",
    "frontier_empty",
    "needs_operator",
    "budget_exhausted",
    "blocked",
    "cancelled",
)

#: The Task lifecycle status a candidate-frontier row stands in.
_PLANNED_STATUS: Final = "PLANNED"

#: The Task statuses whose work is no longer pending, so the plan leaves
#: them out and an edge to one of them orders nothing.
_SETTLED_STATUSES: Final = frozenset({"COMPLETED", "CANCELLED", "FAILED", "DROPPED"})

#: Why a presented request opened no Run: it named no single Task and Run.
_REQUEST_STOP: Final = "run_request_unaddressed"

#: Why a dry run stopped with a frontier it did not dispatch.
_DRY_RUN_STOP: Final = "dry_run"

#: Who a dispatch or retry the pass sends is attributed to. The skill's own
#: slash-prefixed name is not a valid principal key (the daemon's
#: ``PrincipalKey`` pattern is uppercase-anchored and admits no ``/``),
#: so the coordinator carries its own qualified key instead.
_ACTOR_PRINCIPAL: Final = "SKILL-DISPATCH"

MANIFEST = SkillManifest(
    name="/dispatch",
    description="Coordinate one Delivery Batch: bring its ready Tasks to a candidate.",
    runtime=["claude-code", "codex", "opencode"],
    dispatch={"session_policy": "fresh"},
    output_envelope_kind=OUTPUT_SCHEMA,
)


def _budgeted_request(run_request: Mapping[str, Any], budget: int | None) -> dict[str, Any]:
    """Return *run_request* with its capsule's Run token ceiling set to *budget*.

    The presented request already carries its own compiled
    ``capsule.token_budget`` -- the configured default. A caller-named
    ``--budget`` on this invocation overrides it; omitting the flag
    leaves the compiled request exactly as presented.
    """
    if budget is None:
        return dict(run_request)
    capsule = dict(run_request.get("capsule") or {})
    capsule["token_budget"] = budget
    return {**run_request, "capsule": capsule}


def _candidates(args: DispatchArgs, tasks: dict[str, Any], *, sent: set[str]) -> list[str]:
    """Return the Tasks ready on the first stage of the derived plan, in plan order.

    A ready Task stands at ``PLANNED``, was not sent by this pass and, when
    ``--task`` narrowed the pass, is one of the Tasks it names.
    """
    planned = set(row_keys(tasks, status=_PLANNED_STATUS)) - sent
    if args.task:
        planned &= set(args.task)
    derived = derive_concurrency_plan(_graph_tasks(tasks))
    return [key for key in derived.fan_out if key in planned]


def _graph_tasks(tasks: dict[str, Any]) -> tuple[GraphTask, ...]:
    """Return the unfinished Tasks of a Task read model as graph nodes.

    A row states its graph as projected facts; a row that states none
    depends on nothing, claims nothing and does not run alone.
    """
    nodes: list[GraphTask] = []
    for row in tasks.get("rows", ()):
        key = str(row.get("key", ""))
        truth = row.get("status", {})
        if not key or truth.get("value") in _SETTLED_STATUSES:
            continue
        facts = row.get("facts", {})
        nodes.append(
            GraphTask(
                ref=key,
                depends_on=tuple(part for part in facts.get("depends_on", "").split(",") if part),
                write_claims=tuple(
                    part for part in facts.get("write_claims", "").split(",") if part
                ),
                exclusive=facts.get("exclusive") == "true",
            )
        )
    return tuple(nodes)


class DispatchArgs(BaseModel):
    """The accepted invocation of ``/dispatch``, parsed and validated.

    An option this model does not declare fails before anything is
    dispatched, which is the boundary the grammar promises.

    Attributes:
        batch_ref: The Batch to coordinate.
        task: Tasks to restrict the pass to; empty means the whole Batch.
        until: How far the pass runs before it reports.
        provider: The runtime each dispatched Task runs on; the daemon takes
            the first entry of ``runtime.preference`` when omitted.
        resume: A Run reference to resume instead of opening new work.
        run: The Run the one named Task is dispatched under.
        run_request: The compiled dispatch request an operator presents
            for that Run: the compile request, provider documents and
            registry, bindings, capsule, base, lease and prompt.
        budget: The Run token ceiling each dispatched Task's Run is sealed
            with, or that overrides whatever a presented request's capsule
            already carries. ``None`` leaves the compiled value untouched.
        dry_run: Render the plan and record no effect.
        idempotency_key: This request's name; minted when omitted.
        repo_root: The tree to address, when not the daemon's own.
        output: The rendering the caller wants.
    """

    model_config = ConfigDict(extra="forbid")

    batch_ref: str = Field(min_length=1)
    task: tuple[str, ...] = ()
    until: Literal["frontier-empty", "candidate-ready", "attention"] = "frontier-empty"
    provider: Literal["claude-code", "codex"] | None = None
    resume: str | None = None
    run: str | None = None
    run_request: dict[str, Any] | None = None
    budget: int | None = Field(default=None, gt=0)
    dry_run: bool = False
    idempotency_key: str | None = None
    repo_root: str | None = None
    output: OutputRendering = "human"


@register
class DispatchSkill(Skill):
    """Concrete ``/dispatch`` skill.

    Attributes:
        name: The canonical skill name.
    """

    name: SkillName = "/dispatch"

    def __init__(self, *, caller: RpcCaller | None = None) -> None:
        """Bind the transport this pass makes its calls through.

        Args:
            caller: The JSON-RPC seam. ``None`` binds the daemon
                transport at first use, which is the production path.
        """
        self._caller = caller

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Probe the canonical instrument set."""
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        """Coordinate one pass over the Batch and return its report."""
        try:
            args = DispatchArgs.model_validate(dict(ctx.args))
        except ValidationError as error:
            return self._invalid(error)
        caller = self._caller if self._caller is not None else daemon_rpc_caller()
        params: dict[str, Any] = {}
        if args.repo_root is not None:
            params["repo_root"] = args.repo_root
        try:
            batch = RPC_SCOPE.call(caller, BATCH_READ_METHOD, {**params, "key": args.batch_ref})
            tasks = RPC_SCOPE.call(caller, TASK_READ_METHOD, params)
            dispatched = [
                *self._resume(caller, args, params),
                *self._dispatch(caller, args, params),
            ]
        except RpcRefusedError as refused:
            return self._refused(args, refused)
        if args.run_request is None and args.resume is None and not args.dry_run:
            dispatched += self._dispatch_frontier(caller, args, params, tasks)
        return self._report(args, batch=batch, tasks=tasks, dispatched=dispatched)

    def _resume(
        self, caller: RpcCaller, args: DispatchArgs, params: dict[str, Any]
    ) -> list[DispatchedRun]:
        """Resume the named Run, if one was named, and report what came back.

        Args:
            caller: The transport seam.
            args: The validated invocation.
            params: The tree-addressing params every call carries.

        Returns:
            One row per Run addressed; empty when nothing was named.

        Raises:
            RpcRefusedError: The daemon refused the read or the retry.
        """
        if args.resume is None or args.dry_run:
            return []
        RPC_SCOPE.call(caller, RUN_READ_METHOD, {**params, "key": args.resume})
        answer = RPC_SCOPE.call(
            caller,
            RUN_RETRY_METHOD,
            {
                **params,
                "urn": args.resume,
                "actor": _ACTOR_PRINCIPAL,
                "idempotency_key": args.idempotency_key or uuid.uuid4().hex,
            },
        )
        return [
            DispatchedRun(
                task_ref=str(answer.get("run_ref", args.resume)),
                run_ref=args.resume,
                method=RUN_RETRY_METHOD,
                outcome="retried",
            )
        ]

    def _dispatch(
        self, caller: RpcCaller, args: DispatchArgs, params: dict[str, Any]
    ) -> list[DispatchedRun]:
        """Dispatch the one named Task under the named Run, when both were named.

        A presented request opens exactly one Run, so it is honoured only
        beside exactly one Task; with any other count nothing is sent and
        the pass reports the frontier as it stands.

        Args:
            caller: The transport seam.
            args: The validated invocation.
            params: The tree-addressing params every call carries.

        Returns:
            One row for the dispatched Run; empty when nothing was sent.

        Raises:
            RpcRefusedError: The daemon refused the read or the dispatch.
        """
        if args.run is None or args.run_request is None or len(args.task) != 1 or args.dry_run:
            return []
        RPC_SCOPE.call(caller, RUN_READ_METHOD, {**params, "key": args.run})
        RPC_SCOPE.call(
            caller,
            RUN_DISPATCH_METHOD,
            {
                **params,
                **_budgeted_request(args.run_request, args.budget),
                "urn": args.run,
                "actor": _ACTOR_PRINCIPAL,
                "idempotency_key": args.idempotency_key or uuid.uuid4().hex,
            },
        )
        return [
            DispatchedRun(
                task_ref=args.task[0],
                run_ref=args.run,
                method=RUN_DISPATCH_METHOD,
                outcome="dispatched",
            )
        ]

    def _dispatch_frontier(
        self,
        caller: RpcCaller,
        args: DispatchArgs,
        params: dict[str, Any],
        tasks: dict[str, Any],
    ) -> list[DispatchedRun]:
        """Send each Task ready on the first stage to the daemon's Task dispatch verb.

        Each Task is dispatched on its own, so one refusal is reported
        against its Task and leaves the rest of the stage to go out.

        Args:
            caller: The transport seam.
            args: The validated invocation.
            params: The tree-addressing params every call carries.
            tasks: The Task read model the frontier is derived from.

        Returns:
            One row per Task sent, dispatched or refused, in plan order.
        """
        extra: dict[str, Any] = {}
        if args.provider is not None:
            extra["provider"] = args.provider
        if args.budget is not None:
            extra["budget"] = args.budget
        rows: list[DispatchedRun] = []
        for key in _candidates(args, tasks, sent=set()):
            try:
                answer = RPC_SCOPE.call(
                    caller,
                    TASK_DISPATCH_METHOD,
                    {**params, **extra, "task": key, "actor": _ACTOR_PRINCIPAL},
                )
            except RpcRefusedError as refused:
                logger.info(f"dispatch task refused task={key} code={refused.code}")
                rows.append(
                    DispatchedRun(
                        task_ref=key,
                        run_ref="",
                        method=TASK_DISPATCH_METHOD,
                        outcome="refused",
                        refusal_code=refused.code,
                    )
                )
                continue
            rows.append(
                DispatchedRun(
                    task_ref=key,
                    run_ref=str(answer.get("run_ref", "")),
                    method=TASK_DISPATCH_METHOD,
                    outcome="dispatched",
                )
            )
        return rows

    def _report(
        self,
        args: DispatchArgs,
        *,
        batch: dict[str, Any],
        tasks: dict[str, Any],
        dispatched: list[DispatchedRun],
    ) -> SkillResult:
        """Fold the read models into the coordination report."""
        sent = {
            row.task_ref
            for row in dispatched
            if row.method in {RUN_DISPATCH_METHOD, TASK_DISPATCH_METHOD}
        }
        derived = derive_concurrency_plan(_graph_tasks(tasks))
        planned = set(row_keys(tasks, status=_PLANNED_STATUS)) - sent
        if args.task:
            planned &= set(args.task)
        candidates = _candidates(args, tasks, sent=sent)
        cursor = batch.get("header", {}).get("source_cursor")
        plan = ConcurrencyPlan(
            parallel=list(derived.fan_out),
            sequential=list(derived.sequential),
            stages=[list(stage) for stage in derived.stages],
            reasons={ref: list(why) for ref, why in derived.reasons.items()},
        )
        refusals = [row for row in dispatched if row.outcome == "refused"]
        if refusals:
            return self._ok(
                args,
                outcome="blocked",
                cursor=cursor,
                plan=plan,
                dispatched=dispatched,
                frontier=[row.task_ref for row in refusals],
                stopped_on=sorted({str(row.refusal_code) for row in refusals}),
                reason=(
                    f"the daemon refused {len(refusals)} Task(s) sent from batch "
                    f"{args.batch_ref}; each refused row names its Task and code"
                ),
            )
        if not candidates:
            waiting = len(planned)
            started = sum(row.method == TASK_DISPATCH_METHOD for row in dispatched)
            return self._ok(
                args,
                outcome="frontier_empty",
                cursor=cursor,
                plan=plan,
                dispatched=dispatched,
                frontier=[],
                stopped_on=[],
                reason=(
                    f"batch {args.batch_ref} has no undispatched Task standing at "
                    f"{_PLANNED_STATUS} on the first stage of its derived plan"
                    + (f"; {started} dispatched by this pass" if started else "")
                    + (f"; {waiting} wait on earlier stages" if waiting else "")
                ),
            )
        return self._needs_operator(
            args,
            cursor=cursor,
            plan=plan,
            dispatched=dispatched,
            frontier=candidates,
            stop=_DRY_RUN_STOP if args.dry_run else _REQUEST_STOP,
            reason=(
                f"{len(candidates)} Task(s) stand ready on the first stage of batch "
                f"{args.batch_ref}'s derived plan and this pass sent none of them: "
                + (
                    "it is a dry run"
                    if args.dry_run
                    else "a presented --run-request opens a Run only beside one --task and --run"
                )
            ),
        )

    def _ok(
        self,
        args: DispatchArgs,
        *,
        outcome: DispatchOutcome,
        cursor: int | None,
        plan: ConcurrencyPlan,
        dispatched: list[DispatchedRun],
        frontier: list[str],
        stopped_on: list[str],
        reason: str,
    ) -> SkillResult:
        """Build a non-stopping terminal result."""
        body = DispatchBody(
            batch_ref=args.batch_ref,
            source_cursor=cursor,
            plan=plan,
            dispatched=dispatched,
            frontier=frontier,
            stopped_on=stopped_on,
            outcome=outcome,
            reason=reason,
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            next_valid_actions=[
                f"/dispatch {args.batch_ref}"
                if outcome == "blocked"
                else f"/verify {args.batch_ref} --mode all"
            ],
        )

    def _needs_operator(
        self,
        args: DispatchArgs,
        *,
        cursor: int | None,
        plan: ConcurrencyPlan,
        dispatched: list[DispatchedRun],
        frontier: list[str],
        stop: str,
        reason: str,
    ) -> SkillResult:
        """Build the stop-for-an-operator result, which is a valid outcome."""
        body = DispatchBody(
            batch_ref=args.batch_ref,
            source_cursor=cursor,
            plan=plan,
            dispatched=dispatched,
            frontier=frontier,
            stopped_on=[stop],
            outcome="needs_operator",
            reason=reason,
            user_question=UserQuestion(
                question=f"How should the ready Tasks of batch {args.batch_ref} be dispatched?",
                options=[
                    UserQuestionOption(
                        label="dispatch them",
                        description=(
                            "Re-invoke with neither --dry-run nor --run-request; the daemon "
                            "compiles and dispatches each ready Task."
                        ),
                    ),
                    UserQuestionOption(
                        label="stop the pass",
                        description="Leave the Batch where it stands; nothing is dispatched.",
                    ),
                ],
            ),
        )
        return SkillResult(
            status=status_for("needs_operator"),
            body=body.model_dump(mode="json"),
            next_valid_actions=[f"/dispatch {args.batch_ref}"],
        )

    def _refused(self, args: DispatchArgs, refused: RpcRefusedError) -> SkillResult:
        """Turn a daemon refusal into a blocked report carrying its code."""
        body = DispatchBody(
            batch_ref=args.batch_ref,
            stopped_on=[refused.code],
            outcome="blocked",
            reason=f"{refused.method} refused the pass: {refused.detail}",
        )
        return SkillResult(
            status=status_for("blocked"),
            body=body.model_dump(mode="json"),
            repair_commands=[f"eawf doctor --check {refused.code}"],
        )

    def _invalid(self, error: ValidationError) -> SkillResult:
        """Refuse an invocation the grammar does not declare."""
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        body = DispatchBody(
            batch_ref="",
            stopped_on=["invocation_undeclared"],
            outcome="blocked",
            reason=f"the invocation does not parse; check {', '.join(fields)}",
        )
        return SkillResult(
            status=status_for("blocked"),
            body=body.model_dump(mode="json"),
            repair_commands=[INVOCATION_GRAMMAR],
        )


__all__ = [
    "BATCH_READ_METHOD",
    "EFFECTS",
    "INVOCATION_GRAMMAR",
    "MANIFEST",
    "OUTPUT_SCHEMA",
    "RPC_SCOPE",
    "RUN_DISPATCH_METHOD",
    "RUN_READ_METHOD",
    "RUN_RETRY_METHOD",
    "TASK_DISPATCH_METHOD",
    "TASK_READ_METHOD",
    "TERMINAL_OUTCOMES",
    "DispatchArgs",
    "DispatchSkill",
]
