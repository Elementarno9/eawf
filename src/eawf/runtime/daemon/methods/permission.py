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
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.identity import (
    EntityKind,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)
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
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.native_dispatch import stored_run
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_mutator, require_native_call

logger = logging.getLogger(__name__)

#: The verb that records a held call.
PERMISSION_OPEN_METHOD: Final = "runtime.permission.open"

#: The verb a principal decides a held call through.
PERMISSION_DECIDE_METHOD: Final = "runtime.permission.decide"

#: The verb that records every lapse on one Run.
PERMISSION_EXPIRE_METHOD: Final = "runtime.permission.expire"

#: The verb that reports one Run's permissions.
PERMISSION_READ_METHOD: Final = "runtime.permission.read"

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


@register(PERMISSION_READ_METHOD)
async def _read_permissions(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report one Run's permissions with the authority each verb carries."""
    authority = require_native_call(ctx, params)
    args = _params(_RunParams, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(_read, context, args)
    return answer.model_dump(mode="json")


__all__ = [
    "PERMISSION_DECIDE_METHOD",
    "PERMISSION_EXPIRE_METHOD",
    "PERMISSION_OPEN_METHOD",
    "PERMISSION_READ_METHOD",
    "PermissionAnswer",
    "PermissionsAnswer",
]
