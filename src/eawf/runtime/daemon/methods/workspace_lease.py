"""``workspace.lease.*``: the only door a leased worktree is reached through.

Six verbs, and between them they are the whole of what anybody outside
the daemon can do to a lease. A worker can ask to open a path inside the
one it holds and can say it is still alive; it cannot issue itself a
second lease, cannot revoke one, cannot reconcile anything, and above all
cannot move a deadline. ``workspace.lease.heartbeat`` carries the origin
of the beat and refuses an extension from anything but the daemon, so the
refusal is a code on the wire rather than a convention in a prompt.

No response carries a filesystem path except ``workspace.lease.open``,
which is the one call whose entire answer is a path and which produces
none at all without an active lease. Everything else answers with the
opaque ``workspace_handle``, so a worker that never earned a workspace
never learns where one would have been.

The verbs sit behind the epoch-2 fence. A production root is refused
before a byte is read, which is what keeps leases inside canaries and
leaves such a root's epoch-1 worktree ledger the only thing claiming its
worktrees.

A lease does not commit through the native transaction. That path moves
lifecycle records in the tree's one document under a compare-and-swap
revision, and a lease is neither: it is a local-store record about a
directory on this machine, tiered away from the document so it costs the
epoch-1 state schema no version. It takes the same root session and so
the same locks, which is what serialises two issues of one Run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from eawf.kernel.runtime.lease import (
    HeartbeatOrigin,
    LeaseAuthorityError,
    LeaseTransitionError,
    WorkLease,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import native_mutator
from eawf.runtime.workspace.lease import (
    LeaseRefusalCode,
    LeaseRefusedError,
    heartbeat_lease,
    issue_lease,
    open_workspace,
    read_lease,
    reconcile_root,
    revoke_lease,
)

logger = logging.getLogger(__name__)


#: How the six verbs are spelled on the wire.
ISSUE_METHOD: Final = "workspace.lease.issue"
OPEN_METHOD: Final = "workspace.lease.open"
HEARTBEAT_METHOD: Final = "workspace.lease.heartbeat"
REVOKE_METHOD: Final = "workspace.lease.revoke"
RECONCILE_METHOD: Final = "workspace.lease.reconcile"
SHOW_METHOD: Final = "workspace.lease.show"

#: The longest lease the daemon will grant or renew to, in seconds. A
#: ceiling rather than a policy knob: a lease outliving a working day has
#: stopped being a deadline and become a directory nobody reclaims.
MAX_LEASE_SECONDS: Final = 86_400

#: The stable code a request carrying an unreadable stored lease is
#: refused with. It is not a lease refusal: the store, not the request,
#: is what could not answer.
LEASE_STORE_UNREADABLE: Final = "lease_store_unreadable"

LeaseSeconds = Annotated[int, Field(strict=True, ge=1, le=MAX_LEASE_SECONDS)]
BoundedRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class _LeaseParams(BaseModel):
    """The routing key every lease verb shares."""

    model_config = ConfigDict(extra="forbid")

    repo_root: BoundedRef | None = None


class IssueParams(_LeaseParams):
    """What ``workspace.lease.issue`` is asked for.

    Attributes:
        run_ref: The Run the lease is issued to.
        task_ref: The Task whose workspace it is.
        purpose: Why the Run writes. A read-only purpose is refused.
        base: The ref the worktree is materialized at.
        writable_roots: The repository-relative roots a write may land
            under. At least one, because a lease granting no write is a
            lease granting nothing.
        ttl_seconds: How long the lease lasts, bounded by the ceiling so
            a client cannot ask for a lease that never lapses.
    """

    run_ref: BoundedRef
    task_ref: BoundedRef
    purpose: RunPurpose
    base: BoundedRef
    writable_roots: Annotated[tuple[BoundedRef, ...], Field(min_length=1)]
    ttl_seconds: LeaseSeconds


class OpenParams(_LeaseParams):
    """What ``workspace.lease.open`` is asked for.

    Attributes:
        lease_id: The lease the caller claims to hold. Optional so that
            a caller holding none is refused by the gate rather than by
            the parser, which is the refusal the contract is about.
        purpose: Why the caller intends to write.
        relative: The repository-relative path it wants.
    """

    lease_id: BoundedRef | None = None
    purpose: RunPurpose
    relative: BoundedRef


class HeartbeatParams(_LeaseParams):
    """What ``workspace.lease.heartbeat`` is asked for.

    Attributes:
        lease_id: The lease being proven alive.
        origin: Who sent the beat. An agent naming itself honestly is
            refused an extension; one naming itself the daemon is a
            forged origin, which is why the caller is authenticated by
            the transport rather than by this field alone.
        extend_seconds: How much longer the lease should last. Daemon
            origin only.
    """

    lease_id: BoundedRef
    origin: HeartbeatOrigin
    extend_seconds: LeaseSeconds | None = None


class RevokeParams(_LeaseParams):
    """What ``workspace.lease.revoke`` is asked for.

    Attributes:
        lease_id: The lease to withdraw.
        hard: Whether to drop straight to revoked and wait for a
            reconcile, rather than entering the releasing state.
    """

    lease_id: BoundedRef
    hard: bool = True


class ShowParams(_LeaseParams):
    """What ``workspace.lease.show`` is asked for.

    Attributes:
        lease_id: The lease to describe.
    """

    lease_id: BoundedRef


def lease_response(lease: WorkLease) -> dict[str, Any]:
    """Return the wire answer describing one lease.

    Args:
        lease: The lease to describe.

    Returns:
        The JSON-mode record. It names ``workspace_handle`` and no
        directory, because the handle is the whole of what a holder is
        entitled to know about where its workspace lives.
    """
    return lease.model_dump(mode="json")


def _refused(error: LeaseRefusedError) -> DaemonValidationError:
    """Return the wire form of one lease refusal."""
    return DaemonValidationError(f"validation_failed: {error.code.value}: {error.detail}")


def _parsed[ParamsT: _LeaseParams](model: type[ParamsT], params: dict[str, Any]) -> ParamsT:
    """Validate request parameters, or refuse naming only the bad fields.

    The pydantic detail repeats the submitted values, and a refusal that
    reaches terminals and daemon logs must not carry them, so only the
    offending field paths travel.

    Raises:
        DaemonValidationError: The request does not parse.
    """
    try:
        return model.model_validate(params)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise DaemonValidationError(
            f"validation_failed: {LeaseRefusalCode.LEASE_REQUIRED.value}: "
            f"the request is not a valid {model.__name__}; check {', '.join(fields)}"
        ) from error


def _unreadable(error: ValueError, *, lease_id: str) -> DaemonValidationError:
    """Return the wire form of a stored lease that cannot be read."""
    logger.warning(f"_unreadable lease={lease_id} error={type(error).__name__}")
    return DaemonValidationError(
        f"validation_failed: {LEASE_STORE_UNREADABLE}: the stored lease {lease_id!r} "
        "cannot be read, so whether it grants anything is unknown"
    )


@native_mutator(ISSUE_METHOD)
async def _issue(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Materialize an isolated worktree and answer with the lease over it.

    Returns:
        The lease record. The worktree's path is not in it.

    Raises:
        DaemonValidationError: The purpose writes nothing, or the Run
            already holds an active lease.
    """
    request = _parsed(IssueParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        lease = await asyncio.to_thread(
            issue_lease,
            context,
            run_ref=request.run_ref,
            task_ref=request.task_ref,
            purpose=request.purpose,
            base=request.base,
            writable_roots=request.writable_roots,
            now=datetime.now(UTC),
            ttl=timedelta(seconds=request.ttl_seconds),
        )
    except LeaseRefusedError as error:
        logger.info(f"_issue refused code={error.code.value}")
        raise _refused(error) from error
    return lease_response(lease)


@native_mutator(OPEN_METHOD)
async def _open(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Resolve one repository-relative path inside a leased workspace.

    Returns:
        The absolute path, which exists only because an active lease was
        proven first.

    Raises:
        DaemonValidationError: No active lease was held, or the path is
            not one the lease admits.
    """
    request = _parsed(OpenParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        resolved = await asyncio.to_thread(
            open_workspace,
            context,
            purpose=request.purpose,
            lease_id=request.lease_id,
            relative=request.relative,
            now=datetime.now(UTC),
        )
    except LeaseRefusedError as error:
        logger.info(f"_open refused code={error.code.value}")
        raise _refused(error) from error
    except ValueError as error:
        raise _unreadable(error, lease_id=str(request.lease_id)) from error
    return {"lease_id": request.lease_id, "path": str(resolved)}


@native_mutator(HEARTBEAT_METHOD)
async def _heartbeat(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record one liveness beat, extending the deadline for the daemon alone.

    Returns:
        The lease after the beat. An agent-originated beat leaves
        ``expires_at`` reading exactly as it did.

    Raises:
        DaemonValidationError: An agent asked to extend the deadline, or
            the lease is not one a beat reaches.
    """
    request = _parsed(HeartbeatParams, params)
    context = ctx.native_root_context(authority.root)
    extend = None if request.extend_seconds is None else timedelta(seconds=request.extend_seconds)
    try:
        lease = await asyncio.to_thread(
            heartbeat_lease,
            context,
            lease_id=request.lease_id,
            now=datetime.now(UTC),
            origin=request.origin,
            extend=extend,
        )
    except LeaseAuthorityError as error:
        logger.info(f"_heartbeat refused origin={request.origin.value}")
        raise DaemonValidationError(
            f"validation_failed: {LeaseRefusalCode.LEASE_NOT_ACTIVE.value}: {error}"
        ) from error
    except LeaseRefusedError as error:
        raise _refused(error) from error
    except LeaseTransitionError as error:
        raise DaemonValidationError(
            f"validation_failed: {LeaseRefusalCode.LEASE_NOT_ACTIVE.value}: {error}"
        ) from error
    return lease_response(lease)


@native_mutator(REVOKE_METHOD)
async def _revoke(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Withdraw a lease, leaving its residue for the reconcile to judge.

    Returns:
        The lease after the withdrawal.

    Raises:
        DaemonValidationError: The lease is not this root's or is not
            active.
    """
    request = _parsed(RevokeParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        lease = await asyncio.to_thread(
            revoke_lease,
            context,
            lease_id=request.lease_id,
            now=datetime.now(UTC),
            hard=request.hard,
        )
    except LeaseRefusedError as error:
        logger.info(f"_revoke refused code={error.code.value}")
        raise _refused(error) from error
    return lease_response(lease)


@native_mutator(RECONCILE_METHOD)
async def _reconcile(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Clean what one root's lapsed leases left, and keep what is uncertain.

    Returns:
        What the pass found and did. Only the addressed root's leases
        were read, and no ``state.json`` was opened at all.
    """
    _parsed(_LeaseParams, params)
    context = ctx.native_root_context(authority.root)
    report = await asyncio.to_thread(reconcile_root, context, now=datetime.now(UTC))
    return report.model_dump(mode="json")


@native_mutator(SHOW_METHOD)
async def _show(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Describe one lease this root has issued.

    Returns:
        The lease record.

    Raises:
        DaemonValidationError: This root has issued no such lease, or the
            stored record cannot be read.
    """
    request = _parsed(ShowParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        lease = await asyncio.to_thread(read_lease, context, lease_id=request.lease_id)
    except ValueError as error:
        raise _unreadable(error, lease_id=request.lease_id) from error
    if lease is None:
        raise _refused(
            LeaseRefusedError(
                code=LeaseRefusalCode.LEASE_NOT_FOUND,
                detail=f"this root has issued no lease {request.lease_id!r}",
            )
        )
    return lease_response(lease)


__all__ = [
    "HEARTBEAT_METHOD",
    "ISSUE_METHOD",
    "LEASE_STORE_UNREADABLE",
    "MAX_LEASE_SECONDS",
    "OPEN_METHOD",
    "RECONCILE_METHOD",
    "REVOKE_METHOD",
    "SHOW_METHOD",
    "HeartbeatParams",
    "IssueParams",
    "OpenParams",
    "RevokeParams",
    "ShowParams",
    "lease_response",
]
