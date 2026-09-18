"""Turning a native Task into a running provider process, once.

Dispatch is four steps and their order is the contract. The spec is
compiled first, because compilation is the only step that can refuse: a
required capability that is unknown, expired, degraded or mismatched
denies an unattended Run, and it denies it before a worktree has been
materialized or a process started, so the refusal costs nothing to undo.
The lease comes second, the provider process third -- handed the compiled
spec and the sealed capsule and nothing else -- and the worker's
announcement fourth, because a tool grant opens behind an accepted
announcement and never in front of one.

The attempt is a durable record rather than a variable on the stack.
:class:`DispatchAttempt` is appended at each step it completes, so a
daemon that dies between the lease and the spawn leaves the attempt on
the run ledger at the stage it reached. The next dispatch of that Run
reads it, resumes from there, and mints nothing: one attempt identity
survives the restart, which is the whole reason the stage is written down
instead of inferred from whichever side effects happen to exist.

The other half of that identity rule -- what a retry after provider
acceptance may do to it -- lives in
:mod:`eawf.runtime.daemon.native_retry`, which reads the attempt this
module writes.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from eawf.kernel.config.providers import (
    ProviderConfigError,
    ProviderLayer,
    ProviderRegistry,
    parse_provider_configuration,
)
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.capsule import AuthorityCapsule, CapsuleBudget, StopCondition
from eawf.kernel.runtime.compiled import (
    CompiledRunSpec,
    PolicyOverlay,
    RunCompileRequest,
    RuntimeBinding,
)
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES, ControlFact, RunBinding
from eawf.kernel.runtime.handshake import (
    RUNTIME_HANDSHAKE_MISMATCH,
    HandshakeDisposition,
    WorkerHello,
    WorkerHelloFact,
    assess_worker_hello,
)
from eawf.kernel.runtime.lease import (
    LeaseId,
    LeaseStatus,
    WorkLease,
    WorkspaceHandle,
    lease_has_expired,
)
from eawf.kernel.runtime.provider import Digest, SchemaUrn, UniqueToolIds
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictNonNegativeInt, StrictPositiveInt
from eawf.kernel.state.epoch2.run import Run, RunScope
from eawf.kernel.state.epoch2.urns import RunUrn, TaskUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.run_events import (
    hello_facts_of,
    next_hello_sequence,
)
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
    RuntimeSpawnError,
)
from eawf.runtime.runtimes.claude.adapter import ClaudeNativeLauncher
from eawf.runtime.runtimes.codex.adapter import CodexNativeLauncher
from eawf.runtime.workspace.lease import (
    LeaseRefusedError,
    issue_lease,
    root_leases,
    workspace_path,
)
from eawf.workflow.runtime.compile import RunCompileError, compile_run_spec

logger = logging.getLogger(__name__)


#: The verb that drives a Task into a compiled, leased, running Run.
RUN_DISPATCH_METHOD: Final = "runtime.run.dispatch"

#: The verb that decides whether a retry keeps the Run or links a new one.
RUN_RETRY_METHOD: Final = "runtime.run.retry"

#: The ledger-line key prefix a dispatch attempt is filed under. Prefixed
#: for the reason every other runtime line is: the run collection also
#: holds compacted Run records, and a shared key would make one record
#: look like it was in the document and the ledger at once.
_ATTEMPT_KEY_PREFIX: Final = "DSP-"

#: The ledger-line key prefix a retry lineage is filed under.
LINEAGE_KEY_PREFIX: Final = "RTL-"

#: The ledger-line key prefix a contract binding is filed under, matching
#: what ``runtime.run.bind`` writes: dispatch records the same binding at
#: the moment it compiles, so a Run that was dispatched always has one.
BINDING_KEY_PREFIX: Final = "BND-"

#: The status a binding line records.
BINDING_STATUS: Final = "bound"

#: How many random bytes a minted attempt identity carries.
_ATTEMPT_ENTROPY_BYTES: Final = 16

#: One dispatch attempt's own identity.
AttemptId = Annotated[str, StringConstraints(strict=True, pattern=r"^ATT-[0-9a-f]{32}$")]

#: A bounded free-text reference such as a provider session handle.
BoundedRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class DispatchRefusal(StrEnum):
    """The stable codes a dispatch or a retry is refused with.

    A compile rejection keeps its own code rather than being folded into
    one of these: the compiler already names which rule refused, and
    re-coding it here would hide the axis an operator has to fix.
    """

    LAUNCHER_ABSENT = "dispatch_launcher_absent"
    CONTRACT_DRIFT = "dispatch_contract_drift"
    ATTEMPT_IN_FLIGHT = "dispatch_attempt_in_flight"
    PROVIDER_CONFIG_INVALID = "dispatch_provider_config_invalid"
    RUN_NOT_DISPATCHABLE = "dispatch_run_not_dispatchable"
    LEASE_UNAVAILABLE = "dispatch_lease_unavailable"
    SPAWN_FAILED = "dispatch_spawn_failed"
    SUCCESSOR_REQUIRED = "retry_successor_required"
    SUCCESSOR_REFUSED = "retry_successor_refused"
    ATTEMPT_ABSENT = "retry_attempt_absent"


class DispatchStage(StrEnum):
    """How far one dispatch attempt has got.

    The stages are the four steps in their required order, and an attempt
    only ever moves forward through them.
    """

    COMPILED = "compiled"
    LEASED = "leased"
    SPAWNED = "spawned"
    ANNOUNCED = "announced"


#: The stages in the one order dispatch takes them. The tuple is what
#: "in that order" is checked against, rather than the enum's definition
#: order, which nothing stops a later edit from shuffling.
STAGE_ORDER: Final[tuple[DispatchStage, ...]] = (
    DispatchStage.COMPILED,
    DispatchStage.LEASED,
    DispatchStage.SPAWNED,
    DispatchStage.ANNOUNCED,
)

#: The stage from which the provider has accepted the attempt. A retry
#: before it preserves nothing, because nothing was started.
ACCEPTANCE_STAGE: Final = DispatchStage.SPAWNED


def stage_reached(stage: DispatchStage, target: DispatchStage) -> bool:
    """Return whether *stage* is at or past *target* in the fixed order."""
    return STAGE_ORDER.index(stage) >= STAGE_ORDER.index(target)


class DispatchAttempt(BaseModel):
    """One attempt to put a Run in front of a provider, as recorded.

    The record is the attempt's identity. Every line an attempt writes
    repeats ``attempt_ref``, so two lines that disagree about it are two
    attempts, and a restart that resumed correctly leaves exactly one.

    Attributes:
        payload_kind: The discriminator separating an attempt line from a
            control fact, a Run event, a binding and a compacted Run.
        attempt_ref: The attempt's own identity, minted once.
        run_ref: The Run being dispatched.
        task_ref: The Task whose workspace the Run borrows.
        dispatch_key: The idempotency key of the request that minted the
            attempt. A later dispatch presenting another key while this
            one is still in flight is a second dispatcher rather than the
            first one resuming, and is refused.
        stage: How far the attempt has got.
        compiled_spec_digest: The contract digest of the compiled spec.
        authority_capsule_digest: The contract digest of the capsule.
        route_policy_revision: Which revision of the route resolved it.
        provider_kind: Which provider the launch was routed to.
        lease_id: The workspace lease, from the leased stage on.
        workspace_handle: The opaque workspace name, from the same stage.
        provider_session_ref: The provider's own session handle, from the
            spawned stage on.
        subprocess_pid: The child's process id, from the same stage.
        hello_sequence: Which announcement was accepted, at the announced
            stage.
        continuity_attempts: How many resumes this attempt has already
            been granted.
        retry_of_run_ref: The Run this one was linked to as a retry, when
            it was.
        started_at: When the attempt was minted.
        recorded_at: When this line was written.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["dispatch_attempt"] = "dispatch_attempt"
    attempt_ref: AttemptId
    run_ref: RunUrn
    task_ref: TaskUrn
    dispatch_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    stage: DispatchStage
    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    route_policy_revision: StrictPositiveInt
    provider_kind: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    lease_id: LeaseId | None = None
    workspace_handle: WorkspaceHandle | None = None
    provider_session_ref: BoundedRef | None = None
    subprocess_pid: Annotated[int, Field(strict=True, ge=1)] | None = None
    hello_sequence: StrictPositiveInt | None = None
    continuity_attempts: StrictNonNegativeInt = 0
    retry_of_run_ref: RunUrn | None = None
    started_at: UtcDatetime
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _stage_carries_the_facts_it_makes(self) -> Self:
        """Require each stage to name what reaching it produced.

        Raises:
            ValueError: A leased attempt names no lease or no workspace,
                a spawned one names no provider session or pid, or an
                announced one names no announcement. Each absence would
                let a resumed dispatch skip a step it never took.
        """
        required: tuple[tuple[DispatchStage, tuple[str, ...]], ...] = (
            (DispatchStage.LEASED, ("lease_id", "workspace_handle")),
            (DispatchStage.SPAWNED, ("provider_session_ref", "subprocess_pid")),
            (DispatchStage.ANNOUNCED, ("hello_sequence",)),
        )
        for stage, fields in required:
            if not stage_reached(self.stage, stage):
                continue
            missing = tuple(name for name in fields if getattr(self, name) is None)
            if missing:
                raise ValueError(
                    f"a {self.stage.value} attempt reached {stage.value} and names no "
                    f"{', '.join(missing)}"
                )
        if self.retry_of_run_ref is not None and self.retry_of_run_ref == self.run_ref:
            raise ValueError("retry_of_run_ref must name another Run")
        return self


# ---- request and answer shapes ----------------------------------------------


class CapsuleRequest(BaseModel):
    """The capsule fields the compiled spec cannot supply.

    Everything else a capsule carries is derived from the spec, so a
    caller cannot hand a worker authority the compiler did not resolve.

    Attributes:
        criteria_digest: The digest of the criteria the Run is judged on.
        report_schema_ref: The schema the Run's report must satisfy.
        tool_grants: The semantic tools the Run may call. Empty means it
            reaches none, which is a positive statement rather than a
            default a provider can widen.
        tool_denials: The semantic tools withheld; a denial wins.
        stop_conditions: When the Run stops of its own accord.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criteria_digest: Digest
    report_schema_ref: SchemaUrn
    tool_grants: UniqueToolIds = ()
    tool_denials: UniqueToolIds = ()
    stop_conditions: Annotated[tuple[StopCondition, ...], Field(min_length=1)]


class DispatchParams(BaseModel):
    """Params of :data:`RUN_DISPATCH_METHOD`.

    Attributes:
        urn: The Run to dispatch, which the compile request must agree
            with.
        actor: Who asked, as an immutable qualified principal key.
        idempotency_key: The client's name for this request.
        compile_request: What the compiler is asked to compile.
        provider_documents: The ``runtime`` mapping of each provider
            configuration layer, validated through the provider loader
            here rather than trusted as a mapping.
        provider_registry: The installed driver, auth and certification
            facts the configuration is checked against.
        bindings: The installed and certified drivers available to bind.
        policy_overlays: The narrowing layers below the operator's.
        capsule: The capsule fields the spec cannot supply.
        base: The ref the workspace worktree is materialized at.
        lease_ttl_seconds: How long the workspace lease lasts.
        prompt: The rendered prompt the provider process is started with.
    """

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    actor: PrincipalKey
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    compile_request: RunCompileRequest
    provider_documents: dict[str, dict[str, Any]]
    provider_registry: ProviderRegistry
    bindings: Annotated[tuple[RuntimeBinding, ...], Field(min_length=1)]
    policy_overlays: tuple[PolicyOverlay, ...] = ()
    capsule: CapsuleRequest
    base: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    lease_ttl_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)]
    prompt: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200_000)]

    @model_validator(mode="after")
    def _one_run_is_addressed(self) -> Self:
        """Refuse a request whose compile names another Run.

        Raises:
            ValueError: The verb and the compile request disagree, which
                would compile one Run's authority and record it against
                another's binding.
        """
        if self.compile_request.run_ref != self.urn:
            raise ValueError("compile_request.run_ref names another Run than urn")
        return self


class DispatchAnswer(BaseModel):
    """What one dispatch answers with.

    Attributes:
        attempt: The attempt line that now stands on the ledger.
        stage: How far the dispatch got.
        resumed: Whether this call resumed an attempt an earlier one
            left behind rather than minting a new one.
        lease: The workspace lease the Run holds. It carries the opaque
            handle and no directory.
        compiled_spec_digest: The contract digest the provider was handed.
        authority_capsule_digest: The capsule digest it must echo.
        handshake_disposition: What the daemon decided about the worker's
            announcement.
        mismatched_fields: The contract fields the announcement differed
            in, empty on an acceptance.
        refusal_code: Why no tool grant opens, or ``None``.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: dict[str, Any]
    stage: DispatchStage
    resumed: bool
    lease: dict[str, Any]
    compiled_spec_digest: Digest
    authority_capsule_digest: Digest
    handshake_disposition: HandshakeDisposition
    mismatched_fields: tuple[str, ...] = ()
    refusal_code: str | None = None
    reason: str


# ---- launcher routing --------------------------------------------------------


def compile_launchers(
    launchers: Sequence[NativeRunLauncher],
) -> Mapping[str, NativeRunLauncher]:
    """Return *launchers* keyed by provider kind, refusing a broken table.

    Args:
        launchers: One launcher per provider that can be dispatched to.

    Returns:
        A read-only mapping from provider kind to launcher.

    Raises:
        ValueError: No launcher was declared, or two claim one provider
            kind, either of which would make which process a dispatch
            starts depend on iteration order.
    """
    if not launchers:
        raise ValueError("a dispatch table declares at least one launcher")
    table: dict[str, NativeRunLauncher] = {}
    for launcher in launchers:
        if launcher.provider_kind in table:
            raise ValueError(f"two launchers claim provider kind {launcher.provider_kind!r}")
        table[launcher.provider_kind] = launcher
    return table


#: The launchers a dispatch can route to, compiled at import so a table
#: that claims one provider twice is a startup failure rather than a
#: dispatch that starts whichever process was registered last.
NATIVE_LAUNCHERS: Final[Mapping[str, NativeRunLauncher]] = compile_launchers(
    (ClaudeNativeLauncher(), CodexNativeLauncher())
)


def refused(code: DispatchRefusal, detail: str) -> DaemonValidationError:
    """Return the wire form of one dispatch refusal."""
    return DaemonValidationError(f"validation_failed: {code.value}: {detail}")


# ---- durable readers ---------------------------------------------------------


def run_ledger(session: RootSession) -> Path:
    """Return the run collection's append-only ledger in this generation."""
    return session.ledger_path(Epoch2Collection.RUN)


def stored_run(session: RootSession, records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> Run:
    """Return the Run record, from the document or from its ledger.

    A terminal Run is compacted out of the document into the run ledger,
    so a reader that looked only in the document would stop answering for
    exactly the Runs whose outcome matters most.

    Args:
        session: The open root session.
        records: Every line the run ledger holds.
        urn: The Run to read.

    Returns:
        The Run as whichever tier holds it.

    Raises:
        DaemonValidationError: Neither tier holds the record.
    """
    row = document_rows(session.read_document(), Epoch2Collection.RUN).get(urn.entity_key)
    if row is not None:
        return Run.model_validate(row)
    for item in effective_records(records):
        if item.record_key == urn.entity_key and "payload_kind" not in item.payload:
            return Run.model_validate(item.payload)
    raise DaemonValidationError(
        f"validation_failed: identity_not_found: no run record keyed {urn.entity_key!r} "
        "is held by the document or the run ledger"
    )


def control_facts_of(
    records: tuple[LedgerRecord, ...], urn: QualifiedUrn
) -> tuple[ControlFact, ...]:
    """Return one Run's control facts from the run ledger, in sequence order.

    Args:
        records: Every line the run ledger holds.
        urn: The Run to select.

    Returns:
        The Run's control facts, ordered by their own sequence.

    Raises:
        ValidationError: A line claims to be a control fact and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    facts = [
        ControlFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "control"
    ]
    selected = (fact for fact in facts if fact.run_ref == urn)
    return tuple(sorted(selected, key=lambda fact: fact.sequence))


def run_binding_of(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> RunBinding | None:
    """Return one Run's contract binding, or ``None`` when it has none."""
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.run_ref == urn:
            return binding
    return None


def standing_attempt(
    records: tuple[LedgerRecord, ...], urn: QualifiedUrn
) -> DispatchAttempt | None:
    """Return the furthest attempt line this Run has, or ``None``.

    Lines are appended, never replaced, so the standing stage is the last
    one written. Reading the last line rather than the highest stage is
    deliberate: they are the same while the attempt only moves forward,
    and a ledger where they differ is one this reader must not smooth
    over.

    Raises:
        DaemonValidationError: Two lines claim different attempt
            identities for one Run, which means an earlier dispatch minted
            a second attempt instead of resuming the first.
    """
    attempts = [
        DispatchAttempt.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "dispatch_attempt"
    ]
    own = [attempt for attempt in attempts if attempt.run_ref == urn]
    if not own:
        return None
    identities = {attempt.attempt_ref for attempt in own}
    if len(identities) > 1:
        raise refused(
            DispatchRefusal.CONTRACT_DRIFT,
            f"run {urn.entity_key!r} carries {len(identities)} attempt identities, so no one "
            "attempt describes what was dispatched",
        )
    return own[-1]


def active_lease_of(context: Epoch2RootContext, *, run_ref: str, now: datetime) -> WorkLease | None:
    """Return the Run's live lease, or ``None`` when it holds none."""
    for held in root_leases(context):
        if str(held.run_ref) != run_ref:
            continue
        if held.status is LeaseStatus.ACTIVE and not lease_has_expired(held, now=now):
            return held
    return None


# ---- capsule derivation ------------------------------------------------------


def _scope_reference_fields() -> Mapping[str, str]:
    """Return which field each Run scope variant names its subject by.

    Raises:
        ValueError: A variant names no ``<kind>_ref`` field, so the
            capsule could not say what the Run is about.
    """
    fields: dict[str, str] = {}
    for variant in get_args(get_args(RunScope)[0]):
        kind = get_args(variant.model_fields["scope_kind"].annotation)[0]
        name = f"{kind}_ref"
        if name not in variant.model_fields:
            raise ValueError(f"run scope {kind!r} names no {name}")
        fields[kind] = name
    return fields


#: Which field each scope variant carries its subject reference in,
#: compiled at import so a new variant without one fails at startup.
SCOPE_REFERENCE_FIELDS: Final[Mapping[str, str]] = _scope_reference_fields()


def scope_reference(scope: RunScope) -> str:
    """Return the entity reference one Run scope is about."""
    return str(getattr(scope, SCOPE_REFERENCE_FIELDS[scope.scope_kind]))


def seal_capsule(
    *, spec: CompiledRunSpec, request: CapsuleRequest, parent_run_ref: str | None = None
) -> AuthorityCapsule:
    """Seal the authority capsule one compiled spec is dispatched under.

    Every enforcement field is taken from the spec, so the capsule and the
    spec cannot disagree about what the Run may do.

    Args:
        spec: The compiled spec the capsule accompanies.
        request: The capsule fields the spec cannot supply.
        parent_run_ref: The Run this one was forked from, if any.

    Returns:
        The sealed capsule.

    Raises:
        pydantic.ValidationError: A grant names no catalog tool, or the
            scope does not admit a tool that writes.
    """
    return AuthorityCapsule.seal(
        {
            "run_ref": spec.run_ref,
            "parent_run_ref": parent_run_ref,
            "scope_ref": scope_reference(spec.run_scope),
            "scope_digest": spec.scope_digest,
            "agent_role": spec.agent_role,
            "purpose": spec.purpose,
            "authority": spec.authority,
            "tool_grants": request.tool_grants,
            "tool_denials": request.tool_denials,
            "filesystem_policy_ref": spec.sandbox.filesystem_policy_ref,
            "network_policy_ref": spec.sandbox.network_policy_ref,
            "budget": CapsuleBudget(
                cost_microusd=spec.limits.cost_microusd,
                wall_seconds=spec.limits.wall_seconds,
                output_bytes=spec.limits.output_bytes,
                child_runs=spec.limits.child_runs,
                concurrency=spec.limits.concurrency,
            ),
            "criteria_digest": request.criteria_digest,
            "policy_digest": spec.policy_digest,
            "compiled_spec_digest": spec.contract_digest,
            "report_schema_ref": request.report_schema_ref,
            "stop_conditions": request.stop_conditions,
        }
    )


# ---- step one: compile -------------------------------------------------------


def compile_for_dispatch(args: DispatchParams, *, now: datetime) -> CompiledRunSpec:
    """Compile the spec a dispatch will hand the provider.

    This is the first step and the only one that denies: an unattended Run
    whose required capability is unknown, expired, degraded or mismatched
    is refused here, before a worktree exists or a process has been
    started.

    Args:
        args: The validated dispatch request.
        now: The compile stamp.

    Returns:
        The sealed spec.

    Raises:
        DaemonValidationError: The provider configuration does not load,
            or a compile rule refuses the request. Nothing was written,
            leased or spawned.
    """
    try:
        configuration = parse_provider_configuration(
            {_provider_layer(name): document for name, document in args.provider_documents.items()},
            registry=args.provider_registry,
        )
    except ProviderConfigError as error:
        raise refused(DispatchRefusal.PROVIDER_CONFIG_INVALID, str(error)) from error
    try:
        return compile_run_spec(
            args.compile_request,
            configuration=configuration,
            bindings=args.bindings,
            compiled_at=now,
            policy_overlays=args.policy_overlays,
        )
    except RunCompileError as error:
        logger.info(f"compile_for_dispatch refused code={error.code.value}")
        raise DaemonValidationError(f"validation_failed: {error}") from error


def _provider_layer(name: str) -> ProviderLayer:
    """Return *name* as a declared provider layer.

    Raises:
        DaemonValidationError: The name is not one of the layers.
    """
    if name not in get_args(ProviderLayer):
        raise refused(
            DispatchRefusal.PROVIDER_CONFIG_INVALID,
            f"{name!r} is not a provider configuration layer",
        )
    return name  # type: ignore[return-value]


# ---- the ordered driver ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AttemptState:
    """The attempt as the ledger holds it, beside how it got there."""

    attempt: DispatchAttempt
    resumed: bool


def append_attempt(session: RootSession, attempt: DispatchAttempt) -> None:
    """Append one attempt line to the run ledger."""
    append_ledger_record(
        run_ledger(session),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{_ATTEMPT_KEY_PREFIX}{attempt.attempt_ref}",
            status=attempt.stage.value,
            recorded_at=attempt.recorded_at,
            payload=attempt.model_dump(mode="json"),
        ),
    )


def _bind_contract(
    session: RootSession, *, binding: RunBinding, records: tuple[LedgerRecord, ...]
) -> None:
    """Record the contract this Run was dispatched under, once."""
    if run_binding_of(records, binding.run_ref) is not None:
        return
    append_ledger_record(
        run_ledger(session),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{BINDING_KEY_PREFIX}{binding.run_ref.entity_key}",
            status=BINDING_STATUS,
            recorded_at=binding.bound_at,
            payload=binding.model_dump(mode="json"),
        ),
    )


def open_attempt(
    context: Epoch2RootContext,
    args: DispatchParams,
    *,
    spec: CompiledRunSpec,
    capsule: AuthorityCapsule,
    now: datetime,
) -> _AttemptState:
    """Return the Run's one attempt, minting it only if it has none.

    The binding is written in the same locked pass, so a Run that carries
    an attempt always carries the contract that attempt was compiled
    under and a handshake can be judged against it.

    Raises:
        DaemonValidationError: The Run holds no record, it has already
            stopped, or a standing attempt was compiled from a contract
            this call's compile no longer produces.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, args.urn)
        status = reduce_run_control(
            status=run.status, facts=control_facts_of(records, args.urn)
        ).status
        if status in TERMINAL_RUN_STATUSES:
            raise refused(
                DispatchRefusal.RUN_NOT_DISPATCHABLE,
                f"run {args.urn.entity_key!r} is {status.value} and has nothing left to dispatch",
            )
        standing = standing_attempt(records, args.urn)
        if standing is not None:
            if standing.compiled_spec_digest != spec.contract_digest:
                raise refused(
                    DispatchRefusal.CONTRACT_DRIFT,
                    f"run {args.urn.entity_key!r} is mid-dispatch under another compiled "
                    "contract, so resuming it would run the attempt under authority it was "
                    "not started with",
                )
            if standing.dispatch_key != args.idempotency_key:
                raise refused(
                    DispatchRefusal.ATTEMPT_IN_FLIGHT,
                    f"attempt {standing.attempt_ref} is {standing.stage.value} under another "
                    "dispatch key; only the request that opened an attempt resumes it, so a "
                    "second dispatcher cannot start a second worker under one identity",
                )
            return _AttemptState(attempt=standing, resumed=True)
        attempt = DispatchAttempt.model_validate(
            {
                "attempt_ref": f"ATT-{secrets.token_hex(_ATTEMPT_ENTROPY_BYTES)}",
                "run_ref": str(args.urn),
                "task_ref": scope_reference(spec.run_scope),
                "dispatch_key": args.idempotency_key,
                "stage": DispatchStage.COMPILED,
                "compiled_spec_digest": spec.contract_digest,
                "authority_capsule_digest": capsule.contract_digest,
                "route_policy_revision": spec.route_policy_revision,
                "provider_kind": spec.provider_options.provider_kind,
                "started_at": now,
                "recorded_at": now,
            }
        )
        _bind_contract(
            session,
            binding=RunBinding(
                run_ref=args.urn,
                compiled_spec_digest=spec.contract_digest,
                authority_capsule_digest=capsule.contract_digest,
                route_policy_revision=spec.route_policy_revision,
                bound_at=now,
            ),
            records=records,
        )
        append_attempt(session, attempt)
        logger.info(
            f"open_attempt run={args.urn.entity_key!r} attempt={attempt.attempt_ref} "
            f"provider={attempt.provider_kind}"
        )
        return _AttemptState(attempt=attempt, resumed=False)


def lease_workspace(
    context: Epoch2RootContext,
    args: DispatchParams,
    *,
    attempt: DispatchAttempt,
    spec: CompiledRunSpec,
    now: datetime,
) -> tuple[DispatchAttempt, WorkLease]:
    """Issue the Run's workspace lease, or take up the one it already has.

    Raises:
        DaemonValidationError: The lease cannot be issued, or the attempt
            names a lease this root no longer holds as active.
    """
    if stage_reached(attempt.stage, DispatchStage.LEASED):
        held = active_lease_of(context, run_ref=str(args.urn), now=now)
        if held is None or held.lease_id != attempt.lease_id:
            raise refused(
                DispatchRefusal.LEASE_UNAVAILABLE,
                f"attempt {attempt.attempt_ref} was leased {attempt.lease_id!r}, which this "
                "root no longer holds as an active lease",
            )
        return attempt, held
    try:
        lease = issue_lease(
            context,
            run_ref=str(args.urn),
            task_ref=str(attempt.task_ref),
            purpose=spec.purpose,
            base=args.base,
            writable_roots=spec.run_scope.write_set,
            now=now,
            ttl=timedelta(seconds=args.lease_ttl_seconds),
        )
    except LeaseRefusedError as error:
        logger.info(f"lease_workspace refused code={error.code.value}")
        raise refused(DispatchRefusal.LEASE_UNAVAILABLE, error.detail) from error
    moved = attempt.model_copy(
        update={
            "stage": DispatchStage.LEASED,
            "lease_id": lease.lease_id,
            "workspace_handle": lease.workspace_handle,
            "recorded_at": now,
        }
    )
    with context.session([args.urn]) as session:
        append_attempt(session, moved)
    return moved, lease


async def launch_worker(
    context: Epoch2RootContext,
    args: DispatchParams,
    *,
    attempt: DispatchAttempt,
    spec: CompiledRunSpec,
    capsule: AuthorityCapsule,
    lease: WorkLease,
    launchers: Mapping[str, NativeRunLauncher],
    now: datetime,
) -> tuple[DispatchAttempt, NativeLaunchOutcome | None]:
    """Start the provider process with the compiled spec, once.

    An attempt already at the spawned stage is not launched again: the
    provider accepted it, and starting a second process would give one
    attempt identity two workers.

    Raises:
        DaemonValidationError: No launcher serves the compiled provider,
            or the launch failed.
    """
    if stage_reached(attempt.stage, DispatchStage.SPAWNED):
        return attempt, None
    launcher = launchers.get(spec.provider_options.provider_kind)
    if launcher is None:
        raise refused(
            DispatchRefusal.LAUNCHER_ABSENT,
            f"no launcher serves provider {spec.provider_options.provider_kind!r}, so the "
            "compiled spec reaches no process",
        )
    with context.session([args.urn]) as session:
        sequence = next_hello_sequence(
            hello_facts_of(read_ledger_records(run_ledger(session)), args.urn)
        )
    try:
        outcome = await launcher.launch(
            NativeLaunchRequest(
                spec=spec,
                capsule=capsule,
                workspace_handle=lease.workspace_handle,
                workspace=workspace_path(context, handle=lease.workspace_handle),
                prompt=args.prompt,
                hello_sequence=sequence,
            )
        )
    except RuntimeSpawnError as error:
        logger.info(f"launch_worker failed attempt={attempt.attempt_ref}")
        raise refused(DispatchRefusal.SPAWN_FAILED, str(error)) from error
    moved = attempt.model_copy(
        update={
            "stage": DispatchStage.SPAWNED,
            "provider_session_ref": outcome.provider_session_ref,
            "subprocess_pid": outcome.subprocess_pid,
            "recorded_at": now,
        }
    )
    with context.session([args.urn]) as session:
        append_attempt(session, moved)
    return moved, outcome


def accept_announcement(
    context: Epoch2RootContext,
    args: DispatchParams,
    *,
    attempt: DispatchAttempt,
    hello: WorkerHello,
    now: datetime,
) -> tuple[DispatchAttempt, WorkerHelloFact]:
    """Judge the worker's announcement against the Run's recorded binding.

    Raises:
        DaemonValidationError: The Run carries no binding, so there is
            nothing to judge the announcement against.
    """
    with context.session([args.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        binding = run_binding_of(records, args.urn)
        if binding is None:
            raise DaemonValidationError(
                f"validation_failed: identity_not_found: run {args.urn.entity_key!r} has no "
                "contract binding, so there is nothing to check the announcement against"
            )
        outcome = assess_worker_hello(
            hello=hello,
            run_ref=args.urn,
            compiled_spec_digest=binding.compiled_spec_digest,
            authority_capsule_digest=binding.authority_capsule_digest,
        )
        fact = WorkerHelloFact(
            run_ref=args.urn,
            hello=hello,
            disposition=outcome.disposition,
            mismatched_fields=outcome.mismatched_fields,
            actor=args.actor,
            recorded_at=now,
        )
        append_ledger_record(
            run_ledger(session),
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=f"HLO-{hello.hello_sequence}",
                status=outcome.disposition.value,
                recorded_at=now,
                payload=fact.model_dump(mode="json"),
            ),
        )
        moved = attempt
        if outcome.disposition is HandshakeDisposition.ACCEPTED:
            moved = attempt.model_copy(
                update={
                    "stage": DispatchStage.ANNOUNCED,
                    "hello_sequence": hello.hello_sequence,
                    "recorded_at": now,
                }
            )
            append_attempt(session, moved)
    logger.info(
        f"accept_announcement run={args.urn.entity_key!r} "
        f"disposition={outcome.disposition.value} sequence={hello.hello_sequence}"
    )
    return moved, fact


async def dispatch_run(
    context: Epoch2RootContext,
    args: DispatchParams,
    *,
    now: datetime,
    launchers: Mapping[str, NativeRunLauncher] | None = None,
) -> DispatchAnswer:
    """Drive one Run from a Task to an announced worker, in the fixed order.

    Args:
        context: The native context of the canary the Run lives in.
        args: The validated dispatch request.
        now: The stamp every record of this dispatch shares.
        launchers: The launcher table, defaulting to the compiled one.

    Returns:
        The answer, naming the stage reached and whether the attempt was
        resumed rather than minted.

    Raises:
        DaemonValidationError: Any step refused. A refusal before the
            lease leaves the tree exactly as it was.
    """
    table = NATIVE_LAUNCHERS if launchers is None else launchers
    spec = compile_for_dispatch(args, now=now)
    capsule = seal_capsule(spec=spec, request=args.capsule)
    state = open_attempt(context, args, spec=spec, capsule=capsule, now=now)
    attempt, lease = lease_workspace(context, args, attempt=state.attempt, spec=spec, now=now)
    attempt, outcome = await launch_worker(
        context,
        args,
        attempt=attempt,
        spec=spec,
        capsule=capsule,
        lease=lease,
        launchers=table,
        now=now,
    )
    if outcome is None:
        return DispatchAnswer(
            attempt=attempt.model_dump(mode="json"),
            stage=attempt.stage,
            resumed=state.resumed,
            lease=lease.model_dump(mode="json"),
            compiled_spec_digest=spec.contract_digest,
            authority_capsule_digest=capsule.contract_digest,
            handshake_disposition=HandshakeDisposition.ACCEPTED,
            reason=(
                f"attempt {attempt.attempt_ref} had already reached {attempt.stage.value}, "
                "so no second process was started"
            ),
        )
    attempt, fact = accept_announcement(
        context, args, attempt=attempt, hello=outcome.hello, now=now
    )
    accepted = fact.disposition is HandshakeDisposition.ACCEPTED
    return DispatchAnswer(
        attempt=attempt.model_dump(mode="json"),
        stage=attempt.stage,
        resumed=state.resumed,
        lease=lease.model_dump(mode="json"),
        compiled_spec_digest=spec.contract_digest,
        authority_capsule_digest=capsule.contract_digest,
        handshake_disposition=fact.disposition,
        mismatched_fields=fact.mismatched_fields,
        refusal_code=None if accepted else RUNTIME_HANDSHAKE_MISMATCH,
        reason=(
            f"attempt {attempt.attempt_ref} compiled, leased, spawned and was announced"
            if accepted
            else (
                "the worker announced a contract that differs from this Run's binding in "
                f"{', '.join(fact.mismatched_fields)}, so no tool grant opens"
            )
        ),
    )


__all__ = [
    "ACCEPTANCE_STAGE",
    "BINDING_KEY_PREFIX",
    "BINDING_STATUS",
    "LINEAGE_KEY_PREFIX",
    "NATIVE_LAUNCHERS",
    "RUN_DISPATCH_METHOD",
    "RUN_RETRY_METHOD",
    "SCOPE_REFERENCE_FIELDS",
    "STAGE_ORDER",
    "AttemptId",
    "BoundedRef",
    "CapsuleRequest",
    "DispatchAnswer",
    "DispatchAttempt",
    "DispatchParams",
    "DispatchRefusal",
    "DispatchStage",
    "accept_announcement",
    "active_lease_of",
    "append_attempt",
    "compile_for_dispatch",
    "compile_launchers",
    "control_facts_of",
    "dispatch_run",
    "launch_worker",
    "lease_workspace",
    "open_attempt",
    "refused",
    "run_binding_of",
    "run_ledger",
    "scope_reference",
    "seal_capsule",
    "stage_reached",
    "standing_attempt",
    "stored_run",
]
