"""The ``runtime.permission.*`` verbs: record, decide, expire and read a held call.

A provider permission lives on the run ledger beside the Run it holds,
one line per revision, under its own ``PERM-####`` key. These verbs are
its only writers, and none of them touches a pending action: the two
records share a Run and nothing else, so approving one can never answer
the other.

``runtime.permission.open`` records a brokered call the provider is
holding for a principal decision, with the provider's deadline as
observed. ``runtime.permission.decide`` takes a principal's verb. The
hold verb is refused on the record's kind with ``PROTECTED_ACTION_REQUIRED``
before anything is read, because the refusal is a property of what a
provider permission is rather than of its state. A decision that arrives
past the deadline records the expiry first -- the provider has already
denied the call, and that fact is owed a record whether or not the late
answer is -- and is then refused. ``runtime.permission.expire`` records
every lapse on one Run, so a deadline nobody answered still ends in an
explicit ``expired`` resolution rather than in silence.

``runtime.host.permission.request`` is the producer a host harness reaches
through its permission hook. The host names its own session, never a Run, so
the verb binds the call to the one live Run on that vendor session and records
it with the host's decision window as the provider's deadline. The hook that
calls it never waits on an answer: the host keeps asking its own operator, and
the record makes the held call visible here. :func:`expire_lapsed` is what the
daemon's expiry sweep calls, so a lapse is recorded whether or not anyone reads
the Run again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.identity import (
    EntityKind,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.permission import (
    ApprovalAuthority,
    PermissionActionClass,
    PermissionRefusalCode,
    PermissionRefusalError,
    PermissionVerb,
    PrincipalClass,
    ProviderPermission,
    decide_permission,
    expire_permission,
)
from eawf.kernel.runtime.provider import ToolCapabilityId
from eawf.kernel.runtime.semantic import CallId
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import PermissionUrn, RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.native_dispatch import stored_run
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator, require_native_call
from eawf.runtime.runtimes.host_transcript import HostHarness
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

#: The verb that records a held call.
PERMISSION_OPEN_METHOD: Final = "runtime.permission.open"

#: The verb a principal decides a held call through.
PERMISSION_DECIDE_METHOD: Final = "runtime.permission.decide"

#: The verb that records every lapse on one Run.
PERMISSION_EXPIRE_METHOD: Final = "runtime.permission.expire"

#: The verb that reports one Run's permissions.
PERMISSION_READ_METHOD: Final = "runtime.permission.read"

#: The verb a host harness's permission hook records a held call through.
HOST_PERMISSION_REQUEST_METHOD: Final = "runtime.host.permission.request"

#: How long the host waits on a hook's answer to its permission request. The
#: host states no deadline in the request, and the hook that records the call
#: is subscribed without a timeout of its own, so the host's default hook
#: timeout is the window in which any answer from here could reach the call.
HOST_DECISION_WINDOW: Final = timedelta(seconds=60)

#: Who may decide a call the host is holding: the operator the host is asking.
#: The host puts the question to a person at its own prompt, and repository
#: policy is no authority over what that person is being asked.
HOST_AUTHORITY: Final = ApprovalAuthority(
    approve=(PrincipalClass.OPERATOR,), deny=(PrincipalClass.OPERATOR,)
)

#: The host tools whose call writes files, and the ones that reach the network.
#: Every other tool is a plain tool call.
_FILESYSTEM_TOOLS: Final = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_NETWORK_TOOLS: Final = frozenset({"WebFetch", "WebSearch"})

#: The tool-input fields that say in words what a call asks, in preference order.
_SCOPE_FIELDS: Final = ("description", "command", "prompt", "url", "query")

#: What a request scope says in place of words that carry a leak shape.
_WITHHELD_SCOPE: Final = "its input carries a path, an address or a token shape and is withheld"

#: How many hex characters of a digest name a host call.
_CALL_DIGEST_CHARS: Final = 16

_HostText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]

#: The discriminator a permission line carries on the run ledger.
_PAYLOAD_KIND: Final = "provider_permission"

#: The Run statuses a provider can be holding a call in.
_HOLDING_STATUSES: Final = frozenset({RunStatus.RUNNING, RunStatus.SUSPENDED})


class _OpenParams(BaseModel):
    """Params of :data:`PERMISSION_OPEN_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn
    call_ref: CallId
    tool_id: ToolCapabilityId
    action_class: PermissionActionClass
    request_scope: BoundedText
    deadline_at: UtcDatetime
    approval_authority: ApprovalAuthority


class _DecideParams(BaseModel):
    """Params of :data:`PERMISSION_DECIDE_METHOD`."""

    model_config = ConfigDict(extra="forbid")

    urn: PermissionUrn
    verb: PermissionVerb
    principal_class: PrincipalClass
    actor: PrincipalKey
    expected_revision: StrictPositiveInt


class _RunParams(BaseModel):
    """Params of the verbs that address one Run's permissions."""

    model_config = ConfigDict(extra="forbid")

    urn: RunUrn


class HostPermissionRequest(BaseModel):
    """Params of :data:`HOST_PERMISSION_REQUEST_METHOD`, as the host's hook reports them.

    Attributes:
        harness: The host harness holding the call.
        host_session_id: The host's own id of the session the call was made in.
        tool_name: The host tool the call invokes.
        tool_input: The call's input, which the request scope is read from.
    """

    model_config = ConfigDict(extra="forbid")

    harness: HostHarness
    host_session_id: _HostText
    tool_name: _HostText
    tool_input: dict[str, Any] = Field(default_factory=dict)


class PermissionAnswer(BaseModel):
    """One permission as a surface reads it.

    Attributes:
        permission: The latest revision of the record.
        repository_may_approve: Whether the repository principal class may
            approve. False renders the affordance disabled, never absent.
        approve_authority: The classes that may approve, which a disabled
            repository affordance names.
        hold_supported: Always false: the deadline is the provider's.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permission: dict[str, Any]
    repository_may_approve: bool
    approve_authority: tuple[PrincipalClass, ...]
    hold_supported: bool


class PermissionsAnswer(BaseModel):
    """What the read and expire verbs answer with.

    Attributes:
        permissions: The Run's permissions in key order.
        expired: The keys this call recorded an expiry for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    permissions: tuple[PermissionAnswer, ...]
    expired: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Decision:
    """What one decide call wrote, and the refusal it still owes."""

    permission: ProviderPermission
    refusal: PermissionRefusalError | None


def _params[ParamsT: BaseModel](model: type[ParamsT], params: dict[str, Any]) -> ParamsT:
    """Validate request params, dropping the key the fence already used.

    Raises:
        DaemonValidationError: The request does not parse. Only field paths
            are named, so a submitted value never reaches a log.
    """
    try:
        return model.model_validate(
            {key: value for key, value in params.items() if key != REPO_ROOT_PARAM}
        )
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: check {', '.join(fields)}"
        ) from error


def _latest(records: tuple[LedgerRecord, ...]) -> dict[str, ProviderPermission]:
    """Return the latest revision of every permission on the run ledger, by key.

    Raises:
        pydantic.ValidationError: A line claims to be a permission and does
            not validate as one, which means the ledger is corrupt.
    """
    latest: dict[str, ProviderPermission] = {}
    for item in records:
        if item.payload.get("payload_kind") != _PAYLOAD_KIND:
            continue
        permission = ProviderPermission.model_validate(item.payload)
        standing = latest.get(permission.key)
        if standing is None or permission.revision > standing.revision:
            latest[permission.key] = permission
    return latest


def _next_key(taken: dict[str, ProviderPermission]) -> str:
    """Return the ``PERM-####`` key after the highest one ever taken."""
    ordinals = [int(key.split("-", 1)[1]) for key in taken]
    return f"PERM-{max(ordinals, default=0) + 1:04d}"


def _append(session: RootSession, permission: ProviderPermission, *, now: datetime) -> None:
    """Append one revision of *permission* as a line of the run ledger."""
    resolution = permission.resolution
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=permission.key,
            status="open" if resolution is None else resolution.decision,
            recorded_at=now,
            payload=permission.model_dump(mode="json"),
        ),
    )


def _answer(permission: ProviderPermission) -> PermissionAnswer:
    """Project one permission the way a surface renders it."""
    return PermissionAnswer(
        permission=permission.model_dump(mode="json"),
        repository_may_approve=permission.repository_may_approve,
        approve_authority=permission.approval_authority.approve,
        hold_supported=permission.hold_supported,
    )


def _permission_urn(run: QualifiedUrn, key: str) -> QualifiedUrn:
    """Return the URN of permission *key*, in the workspace of Run *run*."""
    return parse_qualified_urn(
        format_qualified_urn(
            workspace_key=run.workspace_key,
            project_key=run.project_key,
            repository_key=None,
            kind=EntityKind.PERMISSION,
            entity_key=key,
        )
    )


def _open(context: Epoch2RootContext, args: _OpenParams, *, now: datetime) -> PermissionAnswer:
    """Record one call the provider is holding for a principal decision.

    Raises:
        DaemonValidationError: The Run is not one a provider can be holding
            a call in, or the provider's deadline has already passed.
    """
    if args.deadline_at <= now:
        raise DaemonValidationError(
            "validation_failed: permission_lapsed: the provider's deadline has already passed, "
            "so there is no held call left to record"
        )
    with context.session([args.urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        run = stored_run(session, records, args.urn)
        if run.status not in _HOLDING_STATUSES:
            raise DaemonValidationError(
                f"validation_failed: illegal_transition: run {args.urn.entity_key} is "
                f"{run.status.value}, so no provider is holding a call in it"
            )
        key = _next_key(_latest(records))
        try:
            permission = ProviderPermission(
                uid=uuid4(),
                key=key,
                urn=_permission_urn(args.urn, key),
                run_ref=args.urn,
                call_ref=args.call_ref,
                tool_id=args.tool_id,
                action_class=args.action_class,
                request_scope=args.request_scope,
                deadline_at=args.deadline_at,
                approval_authority=args.approval_authority,
                opened_at=now,
            )
        except ValidationError as error:
            raise DaemonValidationError(
                f"validation_failed: schema_validation_failed: {error.errors()[0]['msg']}"
            ) from error
        _append(session, permission, now=now)
    logger.info(f"_open key={key} run={args.urn.entity_key}")
    return _answer(permission)


def _decide(context: Epoch2RootContext, args: _DecideParams, *, now: datetime) -> _Decision:
    """Apply one principal's verb, recording a lapse the verb arrived after.

    Raises:
        DaemonValidationError: No permission is recorded under the URN.
    """
    with context.session([args.urn]) as session:
        latest = _latest(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
        permission = latest.get(args.urn.entity_key)
        if permission is None:
            raise DaemonValidationError(
                f"validation_failed: identity_not_found: no provider permission is recorded "
                f"under {args.urn.entity_key}"
            )
        try:
            decided = decide_permission(
                permission,
                verb=args.verb,
                principal_class=args.principal_class,
                principal_ref=args.actor,
                expected_revision=args.expected_revision,
                now=now,
            )
        except PermissionRefusalError as refusal:
            lapsed = expire_permission(permission, now=now)
            if refusal.code is PermissionRefusalCode.LAPSED and lapsed is not None:
                _append(session, lapsed, now=now)
                permission = lapsed
            return _Decision(permission=permission, refusal=refusal)
        _append(session, decided, now=now)
    return _Decision(permission=decided, refusal=None)


def _expire(context: Epoch2RootContext, run: QualifiedUrn, *, now: datetime) -> PermissionsAnswer:
    """Record the expiry of every lapsed, unanswered permission of *run*.

    Args:
        context: The root the Run belongs to.
        run: The Run whose permissions are swept.
        now: The daemon's recording clock, which decides what has lapsed.

    Returns:
        Every permission of the Run after the sweep, and the keys it expired.
    """
    expired: list[str] = []
    with context.session([run]) as session:
        latest = _latest(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
        mine = {key: item for key, item in latest.items() if item.run_ref == run}
        for key in sorted(mine):
            lapsed = expire_permission(mine[key], now=now)
            if lapsed is not None:
                _append(session, lapsed, now=now)
                mine[key] = lapsed
                expired.append(key)
    return PermissionsAnswer(
        permissions=tuple(_answer(mine[key]) for key in sorted(mine)), expired=tuple(expired)
    )


def _read(context: Epoch2RootContext, args: _RunParams) -> PermissionsAnswer:
    """Report one Run's permissions as they stand on the ledger."""
    with context.session([args.urn]) as session:
        latest = _latest(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
    mine = sorted(
        (item for item in latest.values() if item.run_ref == args.urn), key=lambda p: p.key
    )
    return PermissionsAnswer(permissions=tuple(_answer(item) for item in mine))


def _host_call_ref(args: HostPermissionRequest) -> str:
    """Return the id of the host call, derived from what the host asked.

    The host names no id for a call it holds, so the id is a digest of the
    session, the tool and the input: the same request retried names the same
    call, and a different request names a different one.
    """
    body = json.dumps(
        [args.host_session_id, args.tool_name, args.tool_input], sort_keys=True, default=str
    )
    return f"call-{hashlib.sha256(body.encode()).hexdigest()[:_CALL_DIGEST_CHARS]}"


def _host_tool_id(tool_name: str) -> str:
    """Return the host tool's name in the tool-id grammar: lower snake case, letter first."""
    slug = re.sub(r"[^a-z0-9_]", "_", tool_name.lower())
    return (slug if slug[0].isalpha() else f"tool_{slug}")[:64]


def _host_action_class(tool_name: str) -> PermissionActionClass:
    """Return what a host tool's call would do."""
    if tool_name in _FILESYSTEM_TOOLS:
        return PermissionActionClass.FILESYSTEM
    if tool_name in _NETWORK_TOOLS:
        return PermissionActionClass.NETWORK
    return PermissionActionClass.TOOL


def _host_request_scope(args: HostPermissionRequest) -> str:
    """Return what the host asked, in words, cut to a scope's length.

    The words come from the first input field that states the call in words.
    Words carrying a path, an address or a token shape are withheld whole:
    the run ledger refuses those shapes, and a held call must not be the way
    one reaches it.
    """
    words = next(
        (
            value.strip()
            for name in _SCOPE_FIELDS
            if isinstance(value := args.tool_input.get(name), str) and value.strip()
        ),
        None,
    )
    if words is None:
        return f"{args.tool_name} call"
    if scan_state_leaks(words, allowed_emails=default_allowed_emails()):
        return f"{args.tool_name} call; {_WITHHELD_SCOPE}"
    return f"{args.tool_name}: {words}"[:500]


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _host_run(authority: RootAuthority, host_session_id: str) -> RunUrn:
    """Return the one live Run the host session is the vendor session of.

    Raises:
        DaemonValidationError: No live Run is on that session, or several are,
            so the held call cannot be bound to one Run.
    """
    digest = hash_vendor_session_id(host_session_id)
    holding = {status.value for status in _HOLDING_STATUSES}
    live = sorted(
        row["urn"]
        for row in document_rows(
            read_document(_document_path(authority)), Epoch2Collection.RUN
        ).values()
        if row.get("status") in holding
        and isinstance(row.get("vendor_session"), dict)
        and row["vendor_session"].get("session_digest") == digest
    )
    if len(live) != 1:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: {len(live)} live Runs are on the "
            "host session, so the held call is bound to no one Run"
        )
    return parse_qualified_urn(live[0])


def _host_open(
    context: Epoch2RootContext,
    authority: RootAuthority,
    args: HostPermissionRequest,
    *,
    now: datetime,
) -> PermissionAnswer:
    """Record a call the host is holding, bound to the Run on its session.

    A retried hook names the same call, so an open permission of the Run for
    that call is answered again rather than recorded twice.

    Raises:
        DaemonValidationError: The host session is on no one live Run.
    """
    run = _host_run(authority, args.host_session_id)
    call_ref = _host_call_ref(args)
    with context.session([run]) as session:
        latest = _latest(read_ledger_records(session.ledger_path(Epoch2Collection.RUN)))
    standing = next(
        (
            item
            for item in latest.values()
            if item.run_ref == run and item.call_ref == call_ref and item.resolution is None
        ),
        None,
    )
    if standing is not None:
        return _answer(standing)
    return _open(
        context,
        _OpenParams(
            urn=run,
            call_ref=call_ref,
            tool_id=_host_tool_id(args.tool_name),
            action_class=_host_action_class(args.tool_name),
            request_scope=_host_request_scope(args),
            deadline_at=now + HOST_DECISION_WINDOW,
            approval_authority=HOST_AUTHORITY,
        ),
        now=now,
    )


def open_permission_rows(authority: RootAuthority) -> tuple[dict[str, Any], ...]:
    """Return every unresolved permission of the tree as a register row, in key order.

    A permission lives on the run ledger rather than in the document, so a
    register that lists what needs a principal reads it here. Each row is the
    record's latest revision with the status its ledger line was filed under
    and the repository affordance the record resolves.

    Args:
        authority: The fence-cleared tree whose run ledger is read.

    Returns:
        The open permissions; empty when the tree has no run ledger yet.
    """
    path = ledger_path(_document_path(authority), Epoch2Collection.RUN)
    latest = _latest(read_ledger_records(path))
    return tuple(
        {
            **latest[key].model_dump(mode="json"),
            "status": "open",
            "repository_may_approve": latest[key].repository_may_approve,
        }
        for key in sorted(latest)
        if latest[key].resolution is None
    )


def expire_lapsed(context: Epoch2RootContext, *, now: datetime) -> tuple[str, ...]:
    """Record the expiry of every lapsed, unanswered permission in the tree.

    The ledger is read once without a lock to find the Runs owing a lapse;
    each of those Runs is then swept under its own lock, which reads the
    records again, so a decision that landed in between stands.

    Args:
        context: The tree swept.
        now: The daemon's recording clock, which decides what has lapsed.

    Returns:
        The keys this sweep recorded an expiry for.
    """
    path = ledger_path(_document_path(context.require_selected_generation()), Epoch2Collection.RUN)
    owing = sorted(
        {
            str(item.run_ref)
            for item in _latest(read_ledger_records(path)).values()
            if item.resolution is None and item.deadline_at <= now
        }
    )
    expired: list[str] = []
    for run in owing:
        expired.extend(_expire(context, parse_qualified_urn(run), now=now).expired)
    if expired:
        logger.info(f"expire_lapsed root={context.identity.root_id} expired={len(expired)}")
    return tuple(expired)


@native_mutator(PERMISSION_OPEN_METHOD)
async def _open_permission(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record a call the provider is holding, with the provider's deadline."""
    args = _params(_OpenParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_open, context, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(PERMISSION_DECIDE_METHOD)
async def _decide_permission(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Approve or deny a held call; refuse a hold on the record's kind.

    A refusal is raised after the session closes, so an expiry recorded
    on the way to it stands.
    """
    args = _params(_DecideParams, params)
    context = ctx.native_root_context(authority.root)
    outcome = await asyncio.to_thread(_decide, context, args, now=datetime.now(UTC))
    if outcome.refusal is not None:
        logger.info(f"_decide_permission refused code={outcome.refusal.code.value}")
        raise DaemonValidationError(f"validation_failed: {outcome.refusal}")
    return _answer(outcome.permission).model_dump(mode="json")


@native_mutator(PERMISSION_EXPIRE_METHOD)
async def _expire_permissions(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record every lapse on one Run as a provider-decided expiry."""
    args = _params(_RunParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_expire, context, args.urn, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@native_mutator(HOST_PERMISSION_REQUEST_METHOD)
async def _request_host_permission(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record a call a host harness is holding, bound to the Run on its session."""
    args = _params(HostPermissionRequest, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_host_open, context, authority, args, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


@register(PERMISSION_READ_METHOD)
async def _read_permissions(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report one Run's permissions with the authority each verb carries."""
    authority = require_native_call(ctx, params)
    args = _params(_RunParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_read, context, args)
    return answer.model_dump(mode="json")


__all__ = [
    "HOST_AUTHORITY",
    "HOST_DECISION_WINDOW",
    "HOST_PERMISSION_REQUEST_METHOD",
    "PERMISSION_DECIDE_METHOD",
    "PERMISSION_EXPIRE_METHOD",
    "PERMISSION_OPEN_METHOD",
    "PERMISSION_READ_METHOD",
    "HostPermissionRequest",
    "PermissionAnswer",
    "PermissionsAnswer",
    "expire_lapsed",
    "open_permission_rows",
]
