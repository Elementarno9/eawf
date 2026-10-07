"""``runtime.task.dispatch``: turn one Task into one dispatched Run.

The caller names a Task and, optionally, a provider and a token budget.
Everything else is the daemon's to read: the Task record, the provider
preference of the layered config, the installed drivers and their machine
certifications, and the commit the workspace is materialized at. So a
client -- the CLI or the ``/dispatch`` coordinator -- never parses a Task
or a config document, and cannot hand a worker authority the Task never
declared.

The steps run in the order that leaves the least behind on a refusal. The
Task is read and judged first, then the request is compiled, which is the
step that refuses a missing certification or an invalid provider layer;
both refuse with nothing written. Only then is the Task claimed, its Run
created and the Task started under it, each through the registered
lifecycle verb so every guard, receipt and published event is the one an
operator's own ``task claim``/``task start``/``run create`` would produce.
The Run is then driven by :func:`dispatch_run`, which answers once the
worker is announced and leaves the turn to the root's supervisor.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter, ValidationError

from eawf.kernel.config.layered import merge_config
from eawf.kernel.config.providers import ProviderConfigError
from eawf.kernel.config.schema import RuntimeAdapterId
from eawf.kernel.identity import EntityKind, QualifiedUrn, format_entity_key, parse_qualified_urn
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.admission import EconomicsPolicyError, load_economics
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import stored_tasks
from eawf.runtime.daemon.methods.domain_guards import task_run_open
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_dispatch import (
    DispatchAnswer,
    DispatchParams,
    DispatchRefusal,
    DispatchStage,
    dispatch_run,
    refused,
    run_ledger,
)
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator
from eawf.runtime.daemon.provider_facts import installed_provider_facts
from eawf.runtime.daemon.run_supervisor import sweep_after_boot
from eawf.workflow.runtime.task_request import compile_task_request

logger = logging.getLogger(__name__)

#: The verb that dispatches one Task as one new Run.
TASK_DISPATCH_METHOD: Final = "runtime.task.dispatch"

#: The lifecycle verbs the dispatch takes its Task and Run through.
_TASK_CLAIM_METHOD: Final = "domain.task.claim"
_TASK_START_METHOD: Final = "domain.task.start"
_RUN_CREATE_METHOD: Final = "domain.run.create"

#: The runtime adapters a Task can be dispatched to, beside the provider id
#: their shipped launcher's driver manifest names.
DispatchAdapter = Literal["claude-code", "codex"]
_ADAPTER_PROVIDER: Final[dict[str, str]] = {"claude-code": "claude", "codex": "codex"}

#: The statuses a Task never leaves, or leaves only back into the backlog.
_SETTLED_STATUSES: Final = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.DROPPED}
)

#: The statuses a dispatch can claim, start or add a Run to.
_DISPATCHABLE_STATUSES: Final = frozenset(
    {TaskStatus.PLANNED, TaskStatus.CLAIMED, TaskStatus.RUNNING}
)

_RUN_KEY: Final = re.compile(r"^RUN-(\d{8})$")


class TaskDispatchRefusal(StrEnum):
    """The stable codes a Task dispatch is refused with before any Run exists."""

    TASK_SETTLED = "dispatch_task_settled"
    TASK_NOT_READY = "dispatch_task_not_ready"
    TASK_RUN_OPEN = "dispatch_task_run_open"
    PROVIDER_UNSUPPORTED = "dispatch_provider_unsupported"
    BASE_UNRESOLVED = "dispatch_base_unresolved"
    BUDGET_REQUIRED = "dispatch_budget_required"


class TaskDispatchParams(BaseModel):
    """Params of :data:`TASK_DISPATCH_METHOD`.

    Attributes:
        task: The Task's public key or URN.
        actor: Who asked, as an immutable qualified principal key.
        provider: The runtime adapter to dispatch to; ``None`` takes the
            first entry of the layered ``runtime.preference``.
        budget: The Run's token ceiling; ``None`` leaves it uncapped, which
            a token-bounded governor refuses.
    """

    model_config = ConfigDict(extra="forbid")

    task: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]
    actor: PrincipalKey
    provider: DispatchAdapter | None = None
    budget: StrictPositiveInt | None = None


class TaskDispatchAnswer(BaseModel):
    """What one Task dispatch answers with.

    Attributes:
        task_key: The dispatched Task's public key.
        task_ref: Its URN.
        run_key: The Run created for it.
        run_ref: That Run's URN.
        provider: The runtime adapter the Run was dispatched to.
        attempt_ref: The dispatch attempt now standing on the ledger.
        stage: How far the dispatch got.
        dispatch: The answer of the Run dispatch itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_key: str
    task_ref: str
    run_key: str
    run_ref: str
    provider: DispatchAdapter
    attempt_ref: str
    stage: DispatchStage
    dispatch: DispatchAnswer


def _params(params: dict[str, Any]) -> TaskDispatchParams:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse; the detail names
            field paths only, never a submitted value.
    """
    try:
        return TaskDispatchParams.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _refused(code: TaskDispatchRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one Task dispatch refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


def task_urn(document_file: Path, named: str) -> QualifiedUrn:
    """Return the URN of the Task *named* gives the key or URN of.

    Read without the document lock: only the address is taken from it, and
    the Task is judged again under the lock its URN names.

    Raises:
        DaemonValidationError: Neither tier holds a Task by that name.
    """
    document = read_document(document_file)
    lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.TASK))
    payloads = [
        *document_rows(document, Epoch2Collection.TASK).values(),
        *(line.payload for line in effective_records(lines)),
    ]
    for payload in payloads:
        if "urn" in payload and named in {payload.get("key"), payload["urn"]}:
            return parse_qualified_urn(str(payload["urn"]))
    raise DaemonValidationError(
        f"validation_failed: identity_not_found: the tree holds no task {named!r}"
    )


def _dispatchable_task(context: Epoch2RootContext, urn: QualifiedUrn) -> Task:
    """Return the Task at *urn*, if a dispatch may run it.

    Raises:
        DaemonValidationError: The Task is settled or not yet planned, or a
            Run already works for it.
    """
    with context.session([urn]) as session:
        document = session.read_document()
        task = next(row for row in stored_tasks(session) if row.urn == urn)
    if task.status in _SETTLED_STATUSES:
        raise _refused(
            TaskDispatchRefusal.TASK_SETTLED,
            f"task {task.key} is {task.status.value}; its work is no longer pending",
        )
    if task.status not in _DISPATCHABLE_STATUSES:
        raise _refused(
            TaskDispatchRefusal.TASK_NOT_READY,
            f"task {task.key} is {task.status.value}; promote it to PLANNED first",
        )
    if task_run_open(document, task):
        raise _refused(
            TaskDispatchRefusal.TASK_RUN_OPEN,
            f"task {task.key} already has a queued, running or suspended Run",
        )
    return task


def _adapter(repo_root: Path, asked: DispatchAdapter | None) -> DispatchAdapter:
    """Return the adapter the dispatch runs on: the one asked for, else the preferred one.

    Raises:
        DaemonValidationError: The preference ladder is empty, or its first
            entry is an adapter no shipped launcher drives.
    """
    if asked is not None:
        return asked
    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    runtime = merged.get("runtime") or {}
    ladder = TypeAdapter(list[RuntimeAdapterId]).validate_python(
        runtime.get("preference") or runtime.get("adapters") or []
    )
    if not ladder or ladder[0] not in _ADAPTER_PROVIDER:
        raise _refused(
            TaskDispatchRefusal.PROVIDER_UNSUPPORTED,
            f"runtime.preference starts with {ladder[0] if ladder else 'nothing'}, which no "
            "shipped launcher drives; name --provider claude-code or codex",
        )
    first: DispatchAdapter = ladder[0]  # type: ignore[assignment]
    return first


def head_commit(repo_root: Path) -> str:
    """Return the commit ``HEAD`` resolves to in *repo_root*.

    Raises:
        DaemonValidationError: The repository has no resolvable ``HEAD``.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    head = result.stdout.strip()
    if result.returncode != 0 or not head:
        raise _refused(
            TaskDispatchRefusal.BASE_UNRESOLVED,
            "the repository HEAD resolves to no commit, so no workspace base exists",
        )
    return head


def next_run_urn(context: Epoch2RootContext, task: Task) -> QualifiedUrn:
    """Return the Run URN one past every Run key the document and the run ledger hold."""
    with context.session([task.urn]) as session:
        held = set(document_rows(session.read_document(), Epoch2Collection.RUN))
        held.update(line.record_key for line in read_ledger_records(run_ledger(session)))
    ordinals = [int(match.group(1)) for key in held if (match := _RUN_KEY.match(key))]
    return QualifiedUrn(
        workspace_key=task.urn.workspace_key,
        project_key=task.urn.project_key,
        repository_key=task.urn.repository_key,
        kind=EntityKind.RUN,
        entity_key=format_entity_key(EntityKind.RUN, max(ordinals, default=0) + 1),
    )


def dispatch_cursor(context: Epoch2RootContext, urn: QualifiedUrn) -> int:
    """Return the tree's committed canonical sequence, which a create is decided at."""
    with context.session([urn]) as session:
        return int(session.read_document().get(CANONICAL_SEQUENCE_KEY, 0))


def _require_admissible_budget(repo_root: Path, budget: int | None) -> None:
    """Refuse an uncapped Run the in-flight governor can never admit.

    A governor that bounds in-flight tokens holds an unbounded reservation
    against no ceiling, so it queues such a Run forever; refusing here keeps
    that Run, and the Task claim before it, from being written at all.

    Raises:
        DaemonValidationError: The economics table is invalid, or *budget*
            is ``None`` under a token-bounded governor.
    """
    try:
        governor = load_economics(repo_root).governor
    except EconomicsPolicyError as error:
        raise refused(DispatchRefusal.ECONOMICS_INVALID, str(error)) from error
    if budget is None and governor.max_in_flight_tokens is not None:
        raise _refused(
            TaskDispatchRefusal.BUDGET_REQUIRED,
            f"the governor bounds in-flight tokens at {governor.max_in_flight_tokens}, so a "
            "Run with no token budget is never admitted; name a budget",
        )


def _compile(
    context: Epoch2RootContext, task: Task, args: TaskDispatchParams, adapter: DispatchAdapter
) -> DispatchParams:
    """Compile the dispatch request of *task*, refusing before anything is written.

    Raises:
        DaemonValidationError: The economics policy would never admit the
            Run, or the base, the provider layers, the bindings or the
            compiler refused the request.
    """
    tree_root = context.identity.tree_root
    now = datetime.now(UTC)
    _require_admissible_budget(tree_root.parent, args.budget)
    base = head_commit(tree_root.parent)
    try:
        facts = installed_provider_facts(tree_root, now=now)
    except ProviderConfigError as error:
        raise refused(DispatchRefusal.PROVIDER_CONFIG_INVALID, str(error)) from error
    run_urn = next_run_urn(context, task)
    try:
        request = compile_task_request(
            task,
            run_urn,
            provider=_ADAPTER_PROVIDER[adapter],
            config=facts.config,
            registry=facts.registry,
            bindings=facts.bindings,
            budget=args.budget,
            base=base,
            actor=args.actor,
            now=now,
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: task {task.key} compiles to no "
            f"dispatch request; check {', '.join(fields)}"
        ) from error
    return request


async def _lifecycle(
    ctx: MethodContext, routing: dict[str, Any], method: str, params: dict[str, Any]
) -> None:
    """Commit one registered lifecycle verb, raising its refusal as a dispatch refusal.

    Raises:
        DaemonValidationError: The verb answered an error envelope.
    """
    envelope = await methods.dispatch(method, ctx, {**routing, **params})
    if envelope.get("status") == "ok":
        return
    error = (envelope.get("errors") or [{}])[0]
    code = error.get("code", "rpc_refused")
    logger.info(f"task dispatch step refused method={method} code={code}")
    raise DaemonValidationError(f"validation_failed: {code}: {method}: {error.get('message')}")


def _task_now(context: Epoch2RootContext, task: Task) -> Task:
    """Return *task* as the tree holds it now."""
    with context.session([task.urn]) as session:
        return next(row for row in stored_tasks(session) if row.urn == task.urn)


def _publisher(ctx: MethodContext) -> Callable[[Envelope], bool]:
    """Return where the dispatched Run's background transitions are published."""

    def publish(envelope: Envelope) -> bool:
        return publish_projection(ctx.bus, envelope)

    return publish


@native_mutator(TASK_DISPATCH_METHOD)
async def _dispatch_task(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Claim, create, start and dispatch, refusing a settled Task before any Run exists."""
    args = _params(params)
    routing = {REPO_ROOT_PARAM: params[REPO_ROOT_PARAM]} if REPO_ROOT_PARAM in params else {}
    context = ctx.native_root_context(authority.root)
    publish = _publisher(ctx)
    await sweep_after_boot(context, actor=args.actor, publish=publish)
    urn = await asyncio.to_thread(task_urn, document_path(authority), args.task)
    task = await asyncio.to_thread(_dispatchable_task, context, urn)
    adapter = await asyncio.to_thread(_adapter, context.identity.tree_root.parent, args.provider)
    request = await asyncio.to_thread(_compile, context, task, args, adapter)
    run_ref, run_key = str(request.urn), request.urn.entity_key
    if task.status is TaskStatus.PLANNED:
        await _lifecycle(
            ctx,
            routing,
            _TASK_CLAIM_METHOD,
            {
                "urn": str(task.urn),
                "expected_revision": task.revision,
                "idempotency_key": f"task-dispatch-{run_key}-claim",
                "actor": args.actor,
            },
        )
    await _lifecycle(
        ctx,
        routing,
        _RUN_CREATE_METHOD,
        {
            "urn": run_ref,
            "expected_revision": await asyncio.to_thread(dispatch_cursor, context, task.urn),
            "idempotency_key": f"task-dispatch-{run_key}-create",
            "actor": args.actor,
            "spec": {
                "key": run_key,
                "scope": request.compile_request.run_scope.model_dump(mode="json"),
            },
        },
    )
    current = await asyncio.to_thread(_task_now, context, task)
    if current.status is TaskStatus.CLAIMED:
        await _lifecycle(
            ctx,
            routing,
            _TASK_START_METHOD,
            {
                "urn": str(task.urn),
                "expected_revision": current.revision,
                "idempotency_key": f"task-dispatch-{run_key}-start",
                "actor": args.actor,
                "updates": {"active_run_ref": run_ref},
            },
        )
    answer = await dispatch_run(context, request, now=datetime.now(UTC), publish=publish)
    logger.info(
        f"task dispatched task={task.key} run={run_key} adapter={adapter} "
        f"stage={answer.stage.value}"
    )
    return TaskDispatchAnswer(
        task_key=task.key,
        task_ref=str(task.urn),
        run_key=run_key,
        run_ref=run_ref,
        provider=adapter,
        attempt_ref=str(answer.attempt["attempt_ref"]),
        stage=answer.stage,
        dispatch=answer,
    ).model_dump(mode="json")


__all__ = [
    "TASK_DISPATCH_METHOD",
    "DispatchAdapter",
    "TaskDispatchAnswer",
    "TaskDispatchParams",
    "TaskDispatchRefusal",
]
