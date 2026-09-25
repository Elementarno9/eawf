"""The nine checks a semantic call passes before any handler sees it.

A provider process reaches the daemon through one door, and behind that
door sit nine checks in one declared order: the Run's state, the contract
digest it echoes, the positive grant, the hard denial, the typed scope,
the workspace lease, the budget, the idempotency key, and any revocation
that has landed since the Run started. The order is a tuple compiled at
import against the check enum and against the evaluator table, so a check
added without an evaluator, declared twice, or left out of the order is a
startup failure rather than a guard that silently never runs.

Exactly one check answers. The walk stops at the first refusal and names
it on the receipt, so a denied call has one cause an operator can read
rather than a set of complaints. A call that reaches the end is admitted,
and the only non-refusing early exit is the idempotency check, which
returns the original receipt for a call already answered.

Nothing is consulted before the checks. The handler seam is reached only
after the walk returns admitted, which is what makes "denied with zero
filesystem change" a property of the control flow rather than a promise:
there is no path from a refusal to a handler, so a refused call cannot
have written anything.

The capsule travels with the call because the daemon stores only its
digest. That is not the same as letting the caller choose its own
authority: :class:`~eawf.kernel.runtime.capsule.AuthorityCapsule`
recomputes ``contract_digest`` over every field on load, so a capsule
with one grant added hashes to something the Run's recorded binding does
not name, and the contract check refuses it. Widening the capsule is
therefore indistinguishable from forging it.

Role ceilings are applied here rather than carried in the capsule. A
ceiling spelled inside the capsule would be sealed by whoever dispatched
the Run, and a dispatcher able to spell its own ceiling can raise it; the
table in this module is daemon-side and reachable by nothing on the wire,
so the effective grant is the capsule's allow-list intersected with what
the role may produce at all.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, Self

from pydantic import model_validator

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import ControlFact, RunBinding
from eawf.kernel.runtime.lease import LeaseStatus, WorkLease, lease_has_expired
from eawf.kernel.runtime.provider import MUTATING_ROLES, Digest, RuntimeRecord
from eawf.kernel.runtime.semantic import (
    TOOL_SCHEMA_VERSION,
    CallId,
    CoordinationAction,
    IdempotencyKey,
    ReceiptId,
    RunScopedCommandInput,
    SemanticCall,
    SemanticResult,
    SemanticToolError,
    SemanticToolErrorCode,
    SemanticToolId,
    SemanticToolOutput,
    SubmitCoordinationProposalInput,
    error_for,
    tool_contract,
)
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, Run, RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    read_ledger_records,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession, canonical_entity_urn
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.semantic_handlers import (
    BROKERED_TOOLS,
    SEMANTIC_HANDLERS,
    HandlerInputs,
    HandlerRefusalError,
)
from eawf.runtime.workspace.lease import root_leases

logger = logging.getLogger(__name__)


#: The discriminator separating a semantic receipt from the other payload
#: kinds the receipt collection may come to hold.
RECEIPT_PAYLOAD_KIND: Final = "semantic_receipt"

#: How many random bytes a minted receipt identity carries.
_RECEIPT_ENTROPY_BYTES: Final = 8

#: The stable codes a call is refused with before any check runs. They
#: are not tool errors: the request named something the daemon cannot
#: answer for at all, so there is no receipt to attribute an answer to.
IDENTITY_NOT_FOUND: Final = "identity_not_found"
HANDLER_NOT_BROKERED: Final = "handler_not_brokered"


class GatewayTableError(RuntimeError):
    """A gateway table is not total over the vocabulary it covers."""


class SemanticGatewayError(ValueError):
    """A call the gateway cannot answer for, with nothing written.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation, without the code prefix.
    """

    def __init__(self, *, code: str, detail: str) -> None:
        """Build the refusal and the message that leads with its code."""
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PreHandlerCheck(StrEnum):
    """The nine checks, each the one thing it is named for.

    The members are the vocabulary; :data:`PRE_HANDLER_CHECKS` is the
    order they run in. Keeping the two apart is deliberate: the order is
    compiled against this enum at import, so neither can drift from the
    other without the module failing to load.
    """

    RUN_STATE = "run_state"
    CONTRACT_DIGEST = "contract_digest"
    GRANT = "grant"
    DENIAL = "denial"
    SCOPE = "scope"
    LEASE = "lease"
    BUDGET = "budget"
    IDEMPOTENCY = "idempotency"
    REVOCATION = "revocation"


#: The codes each check may refuse with, total over the checks. A check
#: refusing outside its declared vocabulary is a defect the receipt model
#: rejects, so the table binds what is written rather than only what is
#: documented.
REFUSAL_CODES_BY_CHECK: Final[Mapping[PreHandlerCheck, frozenset[SemanticToolErrorCode]]] = (
    MappingProxyType(
        {
            PreHandlerCheck.RUN_STATE: frozenset({SemanticToolErrorCode.RUN_NOT_ACTIVE}),
            PreHandlerCheck.CONTRACT_DIGEST: frozenset({SemanticToolErrorCode.CONTRACT_MISMATCH}),
            PreHandlerCheck.GRANT: frozenset({SemanticToolErrorCode.CAPABILITY_DENIED}),
            PreHandlerCheck.DENIAL: frozenset({SemanticToolErrorCode.CAPABILITY_DENIED}),
            PreHandlerCheck.SCOPE: frozenset({SemanticToolErrorCode.SCOPE_DENIED}),
            PreHandlerCheck.LEASE: frozenset(
                {
                    SemanticToolErrorCode.LEASE_NOT_ACTIVE,
                    SemanticToolErrorCode.STALE_WORKSPACE_GENERATION,
                }
            ),
            PreHandlerCheck.BUDGET: frozenset({SemanticToolErrorCode.BUDGET_EXHAUSTED}),
            PreHandlerCheck.IDEMPOTENCY: frozenset(
                {SemanticToolErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH}
            ),
            PreHandlerCheck.REVOCATION: frozenset({SemanticToolErrorCode.POLICY_REVOKED}),
        }
    )
)

#: The order the checks run in, declared here and compiled at the foot of
#: the module against the enum and the evaluator table.
_DECLARED_ORDER: Final[tuple[PreHandlerCheck, ...]] = (
    PreHandlerCheck.RUN_STATE,
    PreHandlerCheck.CONTRACT_DIGEST,
    PreHandlerCheck.GRANT,
    PreHandlerCheck.DENIAL,
    PreHandlerCheck.SCOPE,
    PreHandlerCheck.LEASE,
    PreHandlerCheck.BUDGET,
    PreHandlerCheck.IDEMPOTENCY,
    PreHandlerCheck.REVOCATION,
)

#: The catalog tools a role's ceiling narrows. A read costs the same
#: whoever asks, and every role may record evidence, report progress and
#: submit its terminal report, so gating those would deny work the role
#: exists to do. What a role is actually defined by is what it produces:
#: a patch, a candidate, a plan, a coordination change.
ROLE_GATED_TOOLS: Final[frozenset[SemanticToolId]] = frozenset(
    {
        SemanticToolId.WORKSPACE_APPLY_PATCH,
        SemanticToolId.SUBMIT_CANDIDATE,
        SemanticToolId.SUBMIT_PLAN,
        SemanticToolId.SUBMIT_COORDINATION_PROPOSAL,
    }
)

#: The two tools that change a repository worktree.
_WRITING_TOOLS: Final[frozenset[SemanticToolId]] = frozenset(
    {SemanticToolId.WORKSPACE_APPLY_PATCH, SemanticToolId.SUBMIT_CANDIDATE}
)

#: Which of the role-gated tools each role may call, total over the role
#: enum. A role with an empty set still reaches every ungated tool; the
#: set names only what the ceiling adds back.
ROLE_TOOL_CEILING: Final[Mapping[AgentSessionRole, frozenset[SemanticToolId]]] = MappingProxyType(
    {
        AgentSessionRole.RESEARCHER: frozenset(),
        AgentSessionRole.PLANNER: frozenset(
            {SemanticToolId.SUBMIT_PLAN, SemanticToolId.SUBMIT_COORDINATION_PROPOSAL}
        ),
        AgentSessionRole.EXECUTOR: _WRITING_TOOLS,
        AgentSessionRole.AUDITOR: frozenset(),
        AgentSessionRole.REVIEWER: frozenset(),
        AgentSessionRole.POLISHER: _WRITING_TOOLS,
        AgentSessionRole.OPERATOR: frozenset({SemanticToolId.SUBMIT_COORDINATION_PROPOSAL}),
        AgentSessionRole.DOMAIN_SPECIALIST: frozenset(),
    }
)

#: The coordination actions that ask the daemon to schedule work into
#: other Runs. The remaining actions change the standing of work that
#: already exists and create nothing.
_FAN_OUT_ACTIONS: Final[frozenset[CoordinationAction]] = frozenset(
    {CoordinationAction.SPLIT_BATCH, CoordinationAction.ESCALATE}
)

#: Executables that start another agent. A scoped command is the one
#: payload that can name a program, so it is the one place a Run can ask
#: for a second agent without a delegation verb to refuse.
AGENT_LAUNCH_COMMANDS: Final[frozenset[str]] = frozenset(
    {"claude", "codex", "opencode", "eawf", "eawfd", "npx", "uvx", "pipx"}
)

#: The tools this daemon answers itself, re-exported from the module that
#: implements them. The set is derived from the handler table there, so a
#: tool becomes brokered by acquiring a handler and by nothing else.


@dataclass(frozen=True, slots=True)
class ScopeRule:
    """How one catalog tool's payload is checked against the Run's scope.

    Attributes:
        subject_field: The payload field naming the entity the call
            attributes its output to, or ``None`` when the call produces
            nothing attributable.
        path_field: The payload field naming repository-relative paths
            the call would touch, or ``None``.
        fan_out: Whether the payload can ask for work to run elsewhere.
    """

    subject_field: str | None = None
    path_field: str | None = None
    fan_out: bool = False


#: How each catalog tool is bounded by the Run's scope, total over the
#: catalog. A tool added without a row would reach the scope check with
#: nothing to compare, which is the same as not being checked.
#:
#: The rows that bound nothing are deliberate. A read costs the same
#: whichever entity it names, and a patch travels by reference, so the
#: paths it would touch are not in the envelope to compare: those land
#: inside the lease's writable roots when the patch is applied, which is
#: the check that can see them.
SCOPE_RULES: Final[Mapping[SemanticToolId, ScopeRule]] = MappingProxyType(
    {
        SemanticToolId.EAWF_STATE_QUERY: ScopeRule(),
        SemanticToolId.REPO_READ: ScopeRule(),
        SemanticToolId.REPO_SEARCH: ScopeRule(),
        SemanticToolId.DIFF_READ: ScopeRule(),
        SemanticToolId.WORKSPACE_APPLY_PATCH: ScopeRule(),
        SemanticToolId.RUN_SCOPED_COMMAND: ScopeRule(fan_out=True),
        SemanticToolId.REPORT_PROGRESS: ScopeRule(),
        SemanticToolId.ATTACH_EVIDENCE: ScopeRule(subject_field="subject_ref"),
        SemanticToolId.ASK_OPERATOR: ScopeRule(subject_field="subject_ref"),
        SemanticToolId.SUBMIT_PLAN: ScopeRule(subject_field="plan_scope_ref"),
        SemanticToolId.SUBMIT_COORDINATION_PROPOSAL: ScopeRule(fan_out=True),
        SemanticToolId.SUBMIT_CANDIDATE: ScopeRule(
            subject_field="task_ref", path_field="changed_paths"
        ),
        SemanticToolId.SUBMIT_REPORT: ScopeRule(),
        SemanticToolId.BUDGET_STATUS: ScopeRule(),
    }
)


class SemanticCallReceipt(RuntimeRecord):
    """The durable record of what one call was answered with.

    Attributes:
        payload_kind: The discriminator separating a receipt line from
            anything else the receipt collection holds.
        schema_version: Version of this record's shape.
        receipt_id: The receipt's own identifier.
        call_id: The call it answers.
        run_ref: The Run the call was made by.
        tool_id: The tool the call named.
        idempotency_key: The key a replay of this call is matched by.
        payload_digest: The digest the key is bound to. A second call
            under the same key carrying another digest is a different
            request wearing the same name.
        refused_check: The check that refused, on a denied receipt and on
            no other.
        result: The envelope the provider process reads.
        recorded_at: When the daemon appended the line.
    """

    payload_kind: Literal["semantic_receipt"] = RECEIPT_PAYLOAD_KIND
    schema_version: Literal["semantic-receipt/v1"] = "semantic-receipt/v1"
    receipt_id: ReceiptId
    call_id: CallId
    run_ref: RunUrn
    tool_id: SemanticToolId
    idempotency_key: IdempotencyKey
    payload_digest: Digest
    refused_check: PreHandlerCheck | None = None
    result: SemanticResult
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _denial_names_the_check_that_caused_it(self) -> Self:
        """Tie a denial to its check, and the check's code to the table.

        Raises:
            ValueError: A denied receipt names no check, a receipt in any
                other status names one, or the error it carries is not a
                code that check is declared to refuse with.
        """
        denied = self.result.status == "denied"
        if denied != (self.refused_check is not None):
            raise ValueError(f"a {self.result.status} receipt disagrees with refused_check")
        if self.refused_check is None:
            return self
        assert self.result.error is not None, "a denied result carries its error"
        admitted = REFUSAL_CODES_BY_CHECK[self.refused_check]
        if self.result.error.code not in admitted:
            raise ValueError(
                f"check {self.refused_check.value!r} does not refuse with "
                f"{self.result.error.code.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _receipt_answers_the_call_it_names(self) -> Self:
        """Refuse a receipt whose envelope answers another call.

        Raises:
            ValueError: The result's call or Run differs from the
                receipt's, which would file one call's answer under
                another's identity.
        """
        if self.result.call_id != self.call_id:
            raise ValueError("result.call_id does not match the receipt's call_id")
        if self.result.run_ref != self.run_ref:
            raise ValueError("result.run_ref does not match the receipt's run_ref")
        return self


@dataclass(frozen=True, slots=True)
class GuardInputs:
    """Everything the nine checks read, gathered once under one lock.

    Attributes:
        call: The envelope the provider process sent.
        capsule: The authority the call claims to run under.
        run: The stored Run record.
        binding: The Run's recorded contract binding, or ``None`` when it
            was never bound.
        control_facts: Every control fact of the Run, in ledger order.
        lease: The Run's one active workspace lease, or ``None``.
        prior: The receipt already filed under this idempotency key.
        calls_so_far: How many receipts the Run already holds.
        now: The instant every deadline and ceiling is judged at.
    """

    call: SemanticCall
    capsule: AuthorityCapsule
    run: Run
    binding: RunBinding | None
    control_facts: tuple[ControlFact, ...]
    lease: WorkLease | None
    prior: SemanticCallReceipt | None
    calls_so_far: int
    now: datetime


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """What the walk of the nine checks decided.

    Attributes:
        verdict: ``admitted`` reaches the handler, ``refused`` never
            does, and ``replayed`` returns an answer already given.
        check: The check that refused, on a refusal and on nothing else.
        error: What the refusal tells the provider process.
        replay: The original receipt a duplicate call is answered with.
    """

    verdict: Literal["admitted", "refused", "replayed"]
    check: PreHandlerCheck | None = None
    error: SemanticToolError | None = None
    replay: SemanticCallReceipt | None = None


def _run_state(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a call from a Run that is not running."""
    if inputs.run.status is RunStatus.RUNNING:
        return None
    return error_for(
        SemanticToolErrorCode.RUN_NOT_ACTIVE,
        message=f"this run is {inputs.run.status.value.lower()} and calls no tool",
    )


def _contract_digest(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a capsule or a call that is not the contract this Run was bound to.

    Three things must agree: the Run's recorded binding, the digest the
    submitted capsule recomputes over its own fields, and the digest the
    call echoes. A capsule edited after sealing fails its own validator
    before reaching here, and a capsule sealed over widened grants hashes
    to something the binding does not name, so the comparison is what
    makes the submitted capsule unforgeable rather than merely declared.
    """
    if inputs.binding is None:
        return error_for(
            SemanticToolErrorCode.CONTRACT_MISMATCH,
            message="this run recorded no contract binding, so no capsule is the one it holds",
        )
    bound = inputs.binding.authority_capsule_digest
    if inputs.capsule.contract_digest != bound:
        return error_for(
            SemanticToolErrorCode.CONTRACT_MISMATCH,
            message="the submitted capsule is not the one this run was bound under",
            field_path="/contract_digest",
        )
    if inputs.call.contract_digest != bound:
        return error_for(
            SemanticToolErrorCode.CONTRACT_MISMATCH,
            message="the call echoes a contract digest this run was not bound under",
            field_path="/contract_digest",
        )
    if canonical_entity_urn(inputs.capsule.run_ref) != canonical_entity_urn(inputs.call.run_ref):
        return error_for(
            SemanticToolErrorCode.CONTRACT_MISMATCH,
            message="the capsule was sealed for another run",
            field_path="/run_ref",
        )
    return None


def _grant(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a tool the capsule never granted, or the role may not produce."""
    tool = inputs.call.tool_id
    if tool.value not in inputs.capsule.tool_grants:
        return error_for(
            SemanticToolErrorCode.CAPABILITY_DENIED,
            message=f"{tool.value} is not in this run's tool grants",
            field_path="/tool_id",
        )
    role = inputs.capsule.agent_role
    if tool in ROLE_GATED_TOOLS and tool not in ROLE_TOOL_CEILING[role]:
        return error_for(
            SemanticToolErrorCode.CAPABILITY_DENIED,
            message=f"the {role.value} role does not produce what {tool.value} produces",
            field_path="/tool_id",
        )
    return None


def _denial(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a tool the capsule hard-denies, whatever else granted it."""
    tool = inputs.call.tool_id
    if tool.value not in inputs.capsule.tool_denials:
        return None
    return error_for(
        SemanticToolErrorCode.CAPABILITY_DENIED,
        message=f"{tool.value} is denied to this run, which outranks any grant of it",
        field_path="/tool_id",
    )


def _scope(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a call that addresses, touches, or fans out past its scope."""
    rule = SCOPE_RULES[inputs.call.tool_id]
    payload = inputs.call.payload
    scope_entity = canonical_entity_urn(_scope_ref(inputs.run))
    if rule.subject_field is not None:
        subject = canonical_entity_urn(getattr(payload, rule.subject_field))
        if subject != scope_entity:
            return error_for(
                SemanticToolErrorCode.SCOPE_DENIED,
                message="the call attributes its output to an entity outside this run's scope",
                field_path=f"/{rule.subject_field}",
            )
    if rule.path_field is not None:
        escaped = _escaped_path(getattr(payload, rule.path_field), inputs.run)
        if escaped is not None:
            return error_for(
                SemanticToolErrorCode.SCOPE_DENIED,
                message=f"{escaped!r} is not in this run's write set",
                field_path=f"/{rule.path_field}",
            )
    if rule.fan_out:
        return _fan_out(payload, capsule=inputs.capsule)
    return None


def _lease(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a write with no active lease, or one aimed at a stale tree.

    The lease is resolved from the Run rather than from the identifier
    the payload carries. A worker is entitled to know that it holds a
    lease and nothing about which one, so a claimed identifier could only
    ever name another Run's workspace or its own; consulting it would add
    a way to be wrong and no way to be right.
    """
    if not tool_contract(inputs.call.tool_id).requires_mutating_task:
        return None
    if inputs.run.scope.purpose not in MUTATING_PURPOSES:
        return error_for(
            SemanticToolErrorCode.LEASE_NOT_ACTIVE,
            message="this run writes nothing, so it holds no workspace to write into",
        )
    if inputs.lease is None:
        return error_for(
            SemanticToolErrorCode.LEASE_NOT_ACTIVE,
            message="this run holds no active workspace lease",
        )
    # One payload in the catalog states which materialization of the
    # workspace it was written against. A call carrying that expectation
    # is checked against the lease, and a call carrying none is not: a
    # generation is a claim about a tree, and a payload that makes no
    # claim has none to be stale.
    expected = getattr(inputs.call.payload, "expected_workspace_generation", None)
    if expected is not None and expected != inputs.lease.workspace_generation:
        return error_for(
            SemanticToolErrorCode.STALE_WORKSPACE_GENERATION,
            message=(
                f"the call expects workspace generation {expected} and the lease is at "
                f"{inputs.lease.workspace_generation}"
            ),
            field_path="/expected_workspace_generation",
        )
    return None


def _budget(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a call from a Run that has spent its wall-clock ceiling.

    Wall time is the one ceiling the daemon measures on its own: it is
    the difference between two stamps it wrote. The remaining ceilings
    are reported by the provider and are checked where they are metered,
    because a ceiling compared against a number nobody measured would
    read as enforced while binding nothing.
    """
    started = inputs.run.started_at
    if started is None:
        return None
    elapsed = (inputs.now - started).total_seconds()
    ceiling = inputs.capsule.budget.wall_seconds
    if elapsed < ceiling:
        return None
    return error_for(
        SemanticToolErrorCode.BUDGET_EXHAUSTED,
        message=f"this run has been running past its ceiling of {ceiling} seconds",
    )


def _idempotency(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a key already bound to another payload.

    A matching key and a matching digest is a duplicate of one request
    and is answered from its receipt, which the walk handles rather than
    this evaluator: a replay is not a refusal.
    """
    prior = inputs.prior
    if prior is None or prior.payload_digest == inputs.call.payload_digest:
        return None
    return error_for(
        SemanticToolErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH,
        message=(
            f"idempotency key {prior.idempotency_key!r} already answers a call carrying "
            "another payload"
        ),
        field_path="/payload_digest",
    )


def _revocation(inputs: GuardInputs) -> SemanticToolError | None:
    """Refuse a call made inside a confirmed control effect's window.

    The stored Run record and its control ledger legitimately disagree
    for as long as it takes a confirmed effect to reach the record it
    moves. The Run-state check reads the record; this one reads the
    effects, so a call arriving in that window is refused rather than
    served under authority an operator has already withdrawn.
    """
    reduced = reduce_run_control(status=inputs.run.status, facts=inputs.control_facts)
    if reduced.status is inputs.run.status:
        return None
    return error_for(
        SemanticToolErrorCode.POLICY_REVOKED,
        message=(
            f"a confirmed control effect has already ended this run as "
            f"{reduced.status.value.lower()}"
        ),
    )


#: Which function answers each check. Total over the enum, compiled at
#: the foot of the module: a check with no evaluator would be walked past
#: in silence, which is the one failure a guard cannot have.
_EVALUATORS: Final[Mapping[PreHandlerCheck, Callable[[GuardInputs], SemanticToolError | None]]] = (
    MappingProxyType(
        {
            PreHandlerCheck.RUN_STATE: _run_state,
            PreHandlerCheck.CONTRACT_DIGEST: _contract_digest,
            PreHandlerCheck.GRANT: _grant,
            PreHandlerCheck.DENIAL: _denial,
            PreHandlerCheck.SCOPE: _scope,
            PreHandlerCheck.LEASE: _lease,
            PreHandlerCheck.BUDGET: _budget,
            PreHandlerCheck.IDEMPOTENCY: _idempotency,
            PreHandlerCheck.REVOCATION: _revocation,
        }
    )
)


def _compile_check_order(
    declared: tuple[PreHandlerCheck, ...],
) -> tuple[PreHandlerCheck, ...]:
    """Return *declared* once it is proven to be the whole check set, once each.

    Args:
        declared: The order the checks are meant to run in.

    Returns:
        *declared* unchanged.

    Raises:
        GatewayTableError: A check repeats, a member of the enum is
            missing from the order, a check declares no refusal code, or
            a check has no evaluator. Each of the four is a guard that
            would never run or would run twice.
    """
    if len(set(declared)) != len(declared):
        raise GatewayTableError("the check order lists one check twice")
    missing = sorted(check.value for check in PreHandlerCheck if check not in declared)
    if missing:
        raise GatewayTableError(f"the check order omits {', '.join(missing)}")
    for check in declared:
        if not REFUSAL_CODES_BY_CHECK.get(check):
            raise GatewayTableError(f"check {check.value!r} declares no refusal code")
        if check not in _EVALUATORS:
            raise GatewayTableError(f"check {check.value!r} has no evaluator")
    return declared


def _compile_scope_rules() -> Mapping[SemanticToolId, ScopeRule]:
    """Return the scope rules once they cover every catalog tool.

    Returns:
        :data:`SCOPE_RULES` unchanged.

    Raises:
        GatewayTableError: A catalog tool has no rule, so the scope check
            would compare it against nothing.
    """
    missing = sorted(tool.value for tool in SemanticToolId if tool not in SCOPE_RULES)
    if missing:
        raise GatewayTableError(f"the scope rules omit {', '.join(missing)}")
    return SCOPE_RULES


def _compile_role_ceiling() -> Mapping[AgentSessionRole, frozenset[SemanticToolId]]:
    """Return the role ceiling once it covers every role and agrees on writing.

    Returns:
        :data:`ROLE_TOOL_CEILING` unchanged.

    Raises:
        GatewayTableError: A role has no row, a row names a tool the
            ceiling does not gate, or the roles that reach a writing tool
            are not exactly the roles declared to mutate. The last is the
            one that matters: two places would otherwise say who writes.
    """
    missing = sorted(role.value for role in AgentSessionRole if role not in ROLE_TOOL_CEILING)
    if missing:
        raise GatewayTableError(f"the role ceiling omits {', '.join(missing)}")
    for role, tools in ROLE_TOOL_CEILING.items():
        ungated = sorted(tool.value for tool in tools - ROLE_GATED_TOOLS)
        if ungated:
            raise GatewayTableError(f"role {role.value!r} names ungated {', '.join(ungated)}")
    writing = {role for role, tools in ROLE_TOOL_CEILING.items() if tools & _WRITING_TOOLS}
    if writing != set(MUTATING_ROLES):
        raise GatewayTableError("the roles reaching a writing tool are not the mutating roles")
    return ROLE_TOOL_CEILING


def _scope_ref(run: Run) -> QualifiedUrn:
    """Return the one entity a Run's scope addresses.

    Args:
        run: The Run whose scope is read.

    Returns:
        The typed reference of its scope variant.

    Raises:
        GatewayTableError: The scope variant carries other than exactly
            one reference, so which entity it addresses is ambiguous.
    """
    fields = [name for name in type(run.scope).model_fields if name.endswith("_ref")]
    if len(fields) != 1:
        raise GatewayTableError(f"run scope {run.scope.scope_kind!r} names {len(fields)} entities")
    reference: QualifiedUrn = getattr(run.scope, fields[0])
    return reference


def _escaped_path(paths: tuple[str, ...], run: Run) -> str | None:
    """Return the first declared path the Run's write set does not contain.

    The comparison is on the repository-relative spelling alone and runs
    before any path is joined to a directory, so an escape is refused
    without the escape ever being resolved against a real tree.

    Args:
        paths: The repository-relative paths the call names.
        run: The Run whose write set bounds them.

    Returns:
        The offending path, or ``None`` when every one is contained.
    """
    allowed = tuple(root.split("/") for root in run.scope.write_set)
    for path in paths:
        parts = path.split("/")
        if not any(parts[: len(root)] == root for root in allowed):
            return path
    return None


def _command_name(argument: str) -> str:
    """Return the executable name one argv token spells, lowercased."""
    tail = argument.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return tail.removesuffix(".exe")


def _launches_an_agent(argv: tuple[str, ...]) -> str | None:
    """Return the agent executable an argv starts, if it starts one.

    Only the program position is read: the first token, which is the
    program however it is spelled, and any later bare word, which is what
    a wrapper such as a package runner executes. A later token carrying a
    separator is a path argument rather than a program, so a command
    reading a file under a directory that happens to share a name with an
    agent is not mistaken for launching one.

    Args:
        argv: The argument vector the call named.

    Returns:
        The offending executable name, or ``None``.
    """
    for index, argument in enumerate(argv):
        bare = "/" not in argument and "\\" not in argument
        if index and not bare:
            continue
        name = _command_name(argument)
        if name in AGENT_LAUNCH_COMMANDS:
            return name
    return None


def _fan_out(payload: Any, *, capsule: AuthorityCapsule) -> SemanticToolError | None:
    """Refuse a call asking for work to run outside this Run.

    An agent never starts an agent. A coordination proposal asks the
    daemon to do it and is bounded by the child-run ceiling, which is
    zero for a mutating task-scoped Run because one Task binds one lease
    and one candidate, so two writers inside it make both unattributable.
    A scoped command that names an agent executable asks for nothing at
    all: it would start a process the daemon never created, so no ceiling
    admits it.
    """
    if isinstance(payload, SubmitCoordinationProposalInput):
        if payload.action in _FAN_OUT_ACTIONS and capsule.budget.child_runs == 0:
            return error_for(
                SemanticToolErrorCode.SCOPE_DENIED,
                message=f"{payload.action.value} asks for work this run may not fan out",
                field_path="/action",
            )
        return None
    if isinstance(payload, RunScopedCommandInput):
        launched = _launches_an_agent(payload.argv)
        if launched is not None:
            return error_for(
                SemanticToolErrorCode.SCOPE_DENIED,
                message=f"a scoped command may not start {launched}; the daemon creates runs",
                field_path="/argv",
            )
    return None


def evaluate_pre_handler_checks(inputs: GuardInputs) -> GuardOutcome:
    """Walk the nine checks in order and return the first answer.

    Args:
        inputs: Everything the checks read, gathered under one lock.

    Returns:
        The outcome. At most one check answers, and it is the earliest in
        :data:`PRE_HANDLER_CHECKS` that has anything to say, so a call
        failing several is refused for one stated reason.
    """
    for check in PRE_HANDLER_CHECKS:
        error = _EVALUATORS[check](inputs)
        if error is not None:
            logger.info(f"evaluate_pre_handler_checks refused check={check.value}")
            return GuardOutcome(verdict="refused", check=check, error=error)
        # The idempotency check is the one with a non-refusing early
        # exit: a duplicate of an answered call leaves with that answer
        # rather than reaching a handler that would repeat its effect.
        if check is PreHandlerCheck.IDEMPOTENCY and inputs.prior is not None:
            return GuardOutcome(verdict="replayed", replay=inputs.prior)
    return GuardOutcome(verdict="admitted")


def serve_semantic_call(
    context: Epoch2RootContext,
    *,
    call: SemanticCall,
    capsule: AuthorityCapsule,
    now: datetime,
) -> SemanticCallReceipt:
    """Guard one call, answer it if it is admitted, and file the receipt.

    The Run is taken from the envelope and from nowhere else. A separate
    addressed identifier would let a call aimed at one Run be judged
    against another's ledger, which is a whole class of confusion that
    cannot arise when there is one source for it.

    Args:
        context: The native context of the root the Run belongs to.
        call: The envelope the provider process sent.
        capsule: The authority it claims to run under.
        now: The instant every deadline and ceiling is judged at.

    Returns:
        The receipt, whether the call was denied, replayed or served. A
        replay returns the original receipt and appends nothing.

    Raises:
        SemanticGatewayError: No Run record answers the envelope, or the
            admitted tool has no handler on this daemon. Nothing is
            written in either case.
        NativeAuthorityRequiredError: The tree is not in epoch 2.
    """
    with context.session([call.run_ref]) as session:
        run_records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        held = _receipts_of(session, call.run_ref)
        key = call.idempotency_key
        inputs = GuardInputs(
            call=call,
            capsule=capsule,
            run=_stored_run(session, run_records, call.run_ref),
            binding=_binding_of(run_records, call.run_ref),
            control_facts=_control_facts(run_records, call.run_ref),
            lease=_active_lease(context, run_ref=call.run_ref, now=now),
            prior=next((item for item in held if item.idempotency_key == key), None),
            calls_so_far=len(held),
            now=now,
        )
        outcome = evaluate_pre_handler_checks(inputs)
        if outcome.replay is not None:
            logger.info(f"serve_semantic_call replayed call={outcome.replay.call_id}")
            return outcome.replay
        receipt = (
            _denied_receipt(call, check=outcome.check, error=outcome.error, now=now)
            if outcome.verdict == "refused"
            else _served_receipt(call, session=session, inputs=inputs, now=now)
        )
        _append_receipt(session, receipt, now=now)
    logger.info(
        f"serve_semantic_call tool={call.tool_id.value} status={receipt.result.status} "
        f"check={'-' if receipt.refused_check is None else receipt.refused_check.value}"
    )
    return receipt


def read_receipt(context: Epoch2RootContext, *, run_ref: str, call_id: str) -> SemanticCallReceipt:
    """Return the receipt one call was answered with.

    Args:
        context: The native context of the root.
        run_ref: The Run the call was made by.
        call_id: The call to describe.

    Returns:
        The stored receipt.

    Raises:
        SemanticGatewayError: This Run has no receipt under that call.
        NativeAuthorityRequiredError: The tree is not in epoch 2.
    """
    with context.session([run_ref]) as session:
        for receipt in _receipts_of(session, run_ref):
            if receipt.call_id == call_id:
                return receipt
    raise SemanticGatewayError(
        code=IDENTITY_NOT_FOUND,
        detail=f"this run holds no receipt for call {call_id!r}",
    )


def _denied_receipt(
    call: SemanticCall,
    *,
    check: PreHandlerCheck | None,
    error: SemanticToolError | None,
    now: datetime,
) -> SemanticCallReceipt:
    """Build the receipt one refused call is answered with."""
    assert check is not None and error is not None, "a refusal names its check and its error"
    return _receipt(call, status="denied", error=error, output=None, check=check, now=now)


def _served_receipt(
    call: SemanticCall, *, session: RootSession, inputs: GuardInputs, now: datetime
) -> SemanticCallReceipt:
    """Answer an admitted call, or refuse to fabricate an answer for it.

    The handler reads through the session the guard already opened. A
    handler that opened one of its own would block on the document lock
    this caller is holding, so the seam passes the pass rather than the
    context.

    Raises:
        SemanticGatewayError: The tool needs a handler this daemon does
            not install, or the handler itself cannot answer the call as
            made. No receipt is filed in either case, because a receipt
            is a record of an answer and there is none.
    """
    handler = SEMANTIC_HANDLERS.get(call.tool_id)
    if handler is None:
        raise SemanticGatewayError(
            code=HANDLER_NOT_BROKERED,
            detail=f"{call.tool_id.value} passed every check and reaches no handler here",
        )
    try:
        output = handler(
            HandlerInputs(
                session=session,
                call=call,
                capsule=inputs.capsule,
                run=inputs.run,
                calls_so_far=inputs.calls_so_far,
                now=now,
                lease=inputs.lease,
            )
        )
    except HandlerRefusalError as error:
        raise SemanticGatewayError(code=error.code, detail=error.detail) from error
    return _receipt(call, status="succeeded", error=None, output=output, check=None, now=now)


def _receipt(
    call: SemanticCall,
    *,
    status: Literal["succeeded", "denied"],
    error: SemanticToolError | None,
    output: SemanticToolOutput | None,
    check: PreHandlerCheck | None,
    now: datetime,
) -> SemanticCallReceipt:
    """Build one receipt around the result envelope the caller reads."""
    receipt_id = f"receipt-{secrets.token_hex(_RECEIPT_ENTROPY_BYTES)}"
    return SemanticCallReceipt(
        receipt_id=receipt_id,
        call_id=call.call_id,
        run_ref=call.run_ref,
        tool_id=call.tool_id,
        idempotency_key=call.idempotency_key,
        payload_digest=call.payload_digest,
        refused_check=check,
        result=SemanticResult(
            call_id=call.call_id,
            run_ref=call.run_ref,
            receipt_id=receipt_id,
            status=status,
            result_schema_version=TOOL_SCHEMA_VERSION,
            bounded_output=output,
            error=error,
            completed_at=now,
        ),
        recorded_at=now,
    )


def _append_receipt(session: RootSession, receipt: SemanticCallReceipt, *, now: datetime) -> None:
    """File one receipt as a line of the root's receipt ledger."""
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key=receipt.call_id,
            status=receipt.result.status,
            recorded_at=now,
            payload=receipt.model_dump(mode="json"),
        ),
    )


def _receipts_of(
    session: RootSession, run_ref: str | QualifiedUrn
) -> tuple[SemanticCallReceipt, ...]:
    """Return one Run's receipts from the receipt ledger, in ledger order.

    Raises:
        ValidationError: A line claims to be a semantic receipt and does
            not validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    wanted = canonical_entity_urn(run_ref)
    records = read_ledger_records(session.ledger_path(Epoch2Collection.RECEIPT))
    receipts = (
        SemanticCallReceipt.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == RECEIPT_PAYLOAD_KIND
    )
    return tuple(item for item in receipts if canonical_entity_urn(item.run_ref) == wanted)


def _stored_run(session: RootSession, records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> Run:
    """Return the Run record, from the document or from its ledger.

    Raises:
        SemanticGatewayError: Neither tier holds the record, so there is
            no Run to attribute an answer to.
    """
    row = document_rows(session.read_document(), Epoch2Collection.RUN).get(urn.entity_key)
    if row is not None:
        return Run.model_validate(row)
    for item in effective_records(records):
        if item.record_key == urn.entity_key and "payload_kind" not in item.payload:
            return Run.model_validate(item.payload)
    raise SemanticGatewayError(
        code=IDENTITY_NOT_FOUND,
        detail=f"no run record keyed {urn.entity_key!r} is held by the document or the run ledger",
    )


def _binding_of(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> RunBinding | None:
    """Return one Run's contract binding, or ``None`` when it has none."""
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.run_ref == urn:
            return binding
    return None


def _control_facts(records: tuple[LedgerRecord, ...], urn: QualifiedUrn) -> tuple[ControlFact, ...]:
    """Return one Run's control facts from the run ledger, in sequence order."""
    facts = [
        ControlFact.model_validate(item.payload)
        for item in records
        if item.payload.get("payload_kind") == "control"
    ]
    return tuple(sorted((fact for fact in facts if fact.run_ref == urn), key=lambda f: f.sequence))


def _active_lease(
    context: Epoch2RootContext, *, run_ref: QualifiedUrn, now: datetime
) -> WorkLease | None:
    """Return the Run's one live workspace lease, or ``None``.

    A Run holds at most one active lease, so the answer is resolved from
    the Run rather than from anything a worker names.
    """
    wanted = canonical_entity_urn(run_ref)
    for held in root_leases(context):
        if canonical_entity_urn(held.run_ref) != wanted:
            continue
        if held.status is LeaseStatus.ACTIVE and not lease_has_expired(held, now=now):
            return held
    return None


PRE_HANDLER_CHECKS: Final[tuple[PreHandlerCheck, ...]] = _compile_check_order(_DECLARED_ORDER)
_compile_scope_rules()
_compile_role_ceiling()


__all__ = [
    "AGENT_LAUNCH_COMMANDS",
    "BROKERED_TOOLS",
    "HANDLER_NOT_BROKERED",
    "IDENTITY_NOT_FOUND",
    "PRE_HANDLER_CHECKS",
    "RECEIPT_PAYLOAD_KIND",
    "REFUSAL_CODES_BY_CHECK",
    "ROLE_GATED_TOOLS",
    "ROLE_TOOL_CEILING",
    "SCOPE_RULES",
    "GatewayTableError",
    "GuardInputs",
    "GuardOutcome",
    "PreHandlerCheck",
    "ScopeRule",
    "SemanticCallReceipt",
    "SemanticGatewayError",
    "evaluate_pre_handler_checks",
    "read_receipt",
    "serve_semantic_call",
]
