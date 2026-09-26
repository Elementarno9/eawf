"""``/dispatch`` skill — coordinate one Delivery Batch over the native Run verbs.

The coordinator brings a Batch's ready Tasks to a candidate by giving
each one its own Run, and keeps the Batch's frontier moving. It writes no
product code, holds no write scope and no lease, and never decides that
work is done: a Run report is not Task completion.

What the pass can read and what it cannot is the whole shape of this
skill. The Batch and Task read models bind the subject at an exact
cursor, and they carry each record's lifecycle status. They do not carry
a Task's dependency edges or its ownership claims, so the *candidate*
frontier is derivable here and the *ready* frontier is not. The pass says
so with a stop code instead of guessing an order: an invented parallelism
plan is the one failure the graph exists to prevent.

The same honesty governs the dispatch arm. Opening a Run needs a compiled
run specification -- provider documents, a certified binding set, an
authority capsule and a rendered prompt -- and no surface this grammar
reaches produces one. Without it the pass stops for an operator rather
than sending a request it would have had to fabricate. An operator who
has compiled one presents it with ``--run-request`` beside the one Task
(``--task``) and the Run it runs under (``--run``); the pass then reads
the Run back and asks the daemon to dispatch exactly that request, and
the dispatched Task leaves the frontier it reports. A ``--budget`` named
on this invocation overrides the presented request's own compiled Run
token ceiling; omitting it leaves that compiled default untouched. The
retry arm is complete: resuming a Run needs the Run reference and
nothing else, so ``--resume`` reaches the daemon.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.surfaces.render.envelope import SkillName
from eawf.workflow.skills._common import probe_skill_instruments
from eawf.workflow.skills.bodies.dispatch import (
    ConcurrencyPlan,
    DispatchBody,
    DispatchedRun,
    DispatchOutcome,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
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


#: The Batch read model the pass binds its subject at.
BATCH_READ_METHOD: Final = "projection.batch.detail.read"

#: The Task read model the candidate frontier is derived from.
TASK_READ_METHOD: Final = "projection.task.detail.read"

#: The Run read model a resumed Run is re-derived from.
RUN_READ_METHOD: Final = "projection.run.detail.read"

#: The verb that opens one Run for one Task.
RUN_DISPATCH_METHOD: Final = "runtime.run.dispatch"

#: The verb that resumes or relinks one Run.
RUN_RETRY_METHOD: Final = "runtime.run.retry"

#: Every JSON-RPC method this skill may address. A call outside the set
#: is refused before the transport is touched.
RPC_SCOPE: Final = RpcScope(
    skill="/dispatch",
    methods=(
        BATCH_READ_METHOD,
        TASK_READ_METHOD,
        RUN_READ_METHOD,
        RUN_DISPATCH_METHOD,
        RUN_RETRY_METHOD,
    ),
)

#: The complete accepted invocation, brackets optional and ``...`` repeatable.
INVOCATION_GRAMMAR: Final = (
    "/dispatch <batch-ref> [--task <ref>...] "
    "[--until <frontier-empty|candidate-ready|attention>] [--max-parallel <N>] "
    "[--provider <id>] [--resume <run-ref>] [--run <run-ref>] [--run-request <compiled>] "
    "[--budget <tokens>] [--dry-run] [--idempotency-key <key>] [--output <human|json|markdown>]"
)

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

#: Why the pass cannot promote the candidate frontier to a ready frontier.
_FRONTIER_STOP: Final = "dependency_proof_unreadable"

#: Why the pass cannot open a Run from this grammar.
_DISPATCH_STOP: Final = "run_request_uncompilable"

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


class DispatchArgs(BaseModel):
    """The accepted invocation of ``/dispatch``, parsed and validated.

    An option this model does not declare fails before anything is
    dispatched, which is the boundary the grammar promises.

    Attributes:
        batch_ref: The Batch to coordinate.
        task: Tasks to restrict the pass to; empty means the whole Batch.
        until: How far the pass runs before it reports.
        max_parallel: The concurrency the plan may use. The resolved
            ceiling is policy, so the pass neither raises nor lowers it.
        provider: The provider id a dispatched Run would bind.
        resume: A Run reference to resume instead of opening new work.
        run: The Run the one named Task is dispatched under.
        run_request: The compiled dispatch request an operator presents
            for that Run: the compile request, provider documents and
            registry, bindings, capsule, base, lease and prompt.
        budget: The Run token ceiling to seal into the presented request's
            capsule, overriding whatever it already carries. ``None``
            leaves the capsule's own compiled value untouched.
        dry_run: Render the plan and record no effect.
        idempotency_key: This request's name; minted when omitted.
        repo_root: The tree to address, when not the daemon's own.
        output: The rendering the caller wants.
    """

    model_config = ConfigDict(extra="forbid")

    batch_ref: str = Field(min_length=1)
    task: tuple[str, ...] = ()
    until: Literal["frontier-empty", "candidate-ready", "attention"] = "frontier-empty"
    max_parallel: int = Field(default=1, ge=1, le=64)
    provider: str | None = None
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

    def _report(
        self,
        args: DispatchArgs,
        *,
        batch: dict[str, Any],
        tasks: dict[str, Any],
        dispatched: list[DispatchedRun],
    ) -> SkillResult:
        """Fold the read models into the coordination report."""
        sent = {row.task_ref for row in dispatched if row.method == RUN_DISPATCH_METHOD}
        candidates = [key for key in row_keys(tasks, status=_PLANNED_STATUS) if key not in sent]
        if args.task:
            wanted = set(args.task)
            candidates = [key for key in candidates if key in wanted]
        cursor = batch.get("header", {}).get("source_cursor")
        if not candidates:
            return self._ok(
                args,
                outcome="frontier_empty",
                cursor=cursor,
                dispatched=dispatched,
                frontier=[],
                stopped_on=[],
                reason=(
                    f"batch {args.batch_ref} has no undispatched Task standing at "
                    f"{_PLANNED_STATUS}, so the candidate frontier is empty"
                ),
            )
        plan = ConcurrencyPlan(
            parallel=candidates[: args.max_parallel],
            sequential=candidates[args.max_parallel :],
            constraint="the resolved concurrency ceiling"
            if len(candidates) > args.max_parallel
            else "",
        )
        return self._needs_operator(
            args,
            cursor=cursor,
            plan=plan,
            dispatched=dispatched,
            frontier=candidates,
            reason=(
                f"{len(candidates)} Task(s) stand at {_PLANNED_STATUS} on batch "
                f"{args.batch_ref}; the read models carry no dependency edge and no compiled run "
                "specification, so the pass stops rather than inventing an order or a request"
            ),
        )

    def _ok(
        self,
        args: DispatchArgs,
        *,
        outcome: DispatchOutcome,
        cursor: int | None,
        dispatched: list[DispatchedRun],
        frontier: list[str],
        stopped_on: list[str],
        reason: str,
    ) -> SkillResult:
        """Build a non-stopping terminal result."""
        body = DispatchBody(
            batch_ref=args.batch_ref,
            source_cursor=cursor,
            dispatched=dispatched,
            frontier=frontier,
            stopped_on=stopped_on,
            outcome=outcome,
            reason=reason,
        )
        return SkillResult(
            status=status_for(outcome),
            body=body.model_dump(mode="json"),
            next_valid_actions=[f"/verify {args.batch_ref} --mode all"],
        )

    def _needs_operator(
        self,
        args: DispatchArgs,
        *,
        cursor: int | None,
        plan: ConcurrencyPlan,
        dispatched: list[DispatchedRun],
        frontier: list[str],
        reason: str,
    ) -> SkillResult:
        """Build the stop-for-an-operator result, which is a valid outcome."""
        body = DispatchBody(
            batch_ref=args.batch_ref,
            source_cursor=cursor,
            plan=plan,
            dispatched=dispatched,
            frontier=frontier,
            stopped_on=[_FRONTIER_STOP, _DISPATCH_STOP],
            outcome="needs_operator",
            reason=reason,
            user_question=UserQuestion(
                question=f"How should the frontier of batch {args.batch_ref} be resolved?",
                options=[
                    UserQuestionOption(
                        label="name the ready Tasks",
                        description="Re-invoke with --task for each Task whose dependencies hold.",
                    ),
                    UserQuestionOption(
                        label="stop the pass",
                        description="Leave the Batch where it stands and resolve the plan defect.",
                    ),
                ],
            ),
        )
        return SkillResult(
            status=status_for("needs_operator"),
            body=body.model_dump(mode="json"),
            next_valid_actions=[f"/dispatch {args.batch_ref} --task <ref> --dry-run"],
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
    "TASK_READ_METHOD",
    "TERMINAL_OUTCOMES",
    "DispatchArgs",
    "DispatchSkill",
]
