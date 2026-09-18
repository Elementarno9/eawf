"""Issuing, holding and reconciling one root's workspace leases.

The lease store is machine-local by construction. Every file this module
writes lives under the addressed root's own tree, so a lease is scoped to
one repository without anything having to remember to scope it: another
root's leases are other files in another tree, and a sweep that walks
this root cannot reach them. A clone carries none of it, which is the
point -- a lease describes a worktree on this machine and means nothing
anywhere else.

Leases are issued inside canaries only. Every mutation here runs inside a
native root session, and a session refuses a tree that is not in epoch 2,
so a production root reaches none of this and keeps the epoch-1 worktree
ledger it already has. The two never claim one directory either: the
epoch-1 ledger materializes under ``.ea/worktrees/`` and a leased
workspace under the root's local store, so neither can clean up the
other's tree.

Reconcile is the half that must not be clever. A worktree whose git
status is empty and whose HEAD still reads as the commit it was
materialized at holds nothing anybody could want, so it is removed. Any
other answer -- a dirty tree, a moved HEAD, a directory that is not there
any more, a git that will not report -- is uncertain, and uncertain
residue is quarantined exactly where it lies under a recovery handle.
Nothing in this module deletes a byte it has not first shown to be
worthless.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import eawf.runtime.worktree.git as git
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.lease import (
    INITIAL_LEASE_STATUS,
    LEASE_SCHEMA_VERSION,
    HeartbeatOrigin,
    LeaseAuthorityError,
    LeaseStatus,
    QuarantineReason,
    RecoveryHandle,
    WorkLease,
    WorkspaceHandle,
    apply_lease_transition,
    lease_has_expired,
    next_workspace_generation,
    record_heartbeat,
    renew_lease,
    write_admitted,
)
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.writer import atomic_write_json
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, canonical_entity_urn
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)


#: Where one root files its leases, relative to the tree root. Under the
#: local store, because a lease names a worktree on this machine.
LEASE_LOCATOR: Final = "local/epoch2/leases"

#: Where leased worktrees are materialized. Deliberately apart from the
#: epoch-1 ``.ea/worktrees/`` ledger so the two never claim one path.
WORKSPACE_LOCATOR: Final = "local/epoch2/workspaces"

#: Where the record of quarantined residue is filed. The residue itself
#: stays where it lies; this is only how it is found again.
QUARANTINE_LOCATOR: Final = "local/epoch2/quarantine"

#: The branch namespace a leased worktree is materialized on. Opaque, so
#: it cannot collide with an operator's feature branches.
LEASE_BRANCH_PREFIX: Final = "lease"

#: Version of the persisted quarantine record shape.
QUARANTINE_SCHEMA_VERSION: Final = "1"

#: How many residue lines a quarantine record keeps. The record is a
#: pointer to residue that is still on disk, not a copy of it.
RESIDUE_SAMPLE_WIDTH: Final = 32

#: The statuses whose residue a pass judges freely. Each leads to
#: ``RECONCILING``, whose two exits cover both verdicts, so a lease in one
#: of them always leaves the pass terminal. An active lease joins them by
#: expiring; the rest are leases a previous pass or a crash left partway.
RECONCILABLE_STATUSES: Final[frozenset[LeaseStatus]] = frozenset(
    {LeaseStatus.EXPIRED, LeaseStatus.REVOKED, LeaseStatus.RECONCILING}
)

#: The status whose single exit is conditional. A withdrawing lease
#: releases only once its residue is proven safe, and the machine gives it
#: no quarantine edge, so an unproven one waits for a later pass.
RELEASE_ONLY_STATUS: Final = LeaseStatus.REVOKING


class ReconcileOutcome(StrEnum):
    """What one reconcile pass did to one lease.

    Distinct from the lease's own status because the two answer different
    questions: a lease that reads ``released`` may have been released by
    an earlier pass, and counting it again would report work this pass did
    not do.
    """

    RELEASED = "released"
    QUARANTINED = "quarantined"
    UNTOUCHED = "untouched"


class LeaseRefusalCode(StrEnum):
    """The stable codes a lease request is refused with."""

    LEASE_REQUIRED = "lease_required"
    LEASE_NOT_FOUND = "lease_not_found"
    LEASE_NOT_ACTIVE = "lease_not_active"
    LEASE_EXPIRED = "lease_expired"
    PURPOSE_NOT_MUTATING = "purpose_not_mutating"
    SCOPE_ESCAPE = "scope_escape"
    LEASE_ALREADY_ACTIVE = "lease_already_active"


class LeaseRefusedError(ValueError):
    """One lease request was refused, with nothing written.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation, without the code prefix.
    """

    def __init__(self, *, code: LeaseRefusalCode, detail: str) -> None:
        """Build the refusal and the message that leads with its code."""
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


class QuarantineRecord(BaseModel):
    """The handle by which residue nobody could vouch for is found again.

    The record carries no filesystem path. A quarantine is read beside
    the root it belongs to, and the workspace handle locates the residue
    within it, so writing the absolute location here would add a host
    path to a file for no reading anybody needs.

    Attributes:
        schema_version: Version of this record's shape.
        recovery_handle: The handle the lease points at.
        lease_id: The lease whose residue this is.
        workspace_handle: Which workspace under the root holds it.
        workspace_generation: Which materialization of it.
        branch: The branch it was materialized on.
        base_commit: The commit it was materialized at.
        reason: Why it could not be shown to be worthless.
        residue: A bounded sample of what was observed, as git reported
            it. Repository-relative, because git porcelain is.
        workspace_present: Whether the directory was there to look at.
        quarantined_at: When the reconcile pass decided this.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16)]
    recovery_handle: RecoveryHandle
    lease_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    workspace_handle: WorkspaceHandle
    workspace_generation: int
    branch: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
    base_commit: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
    reason: QuarantineReason
    residue: tuple[str, ...] = ()
    workspace_present: bool
    quarantined_at: UtcDatetime


class ReconcileReport(BaseModel):
    """What one root-scoped reconcile pass found and did.

    Attributes:
        root_id: The root the pass walked. Only this root's leases were
            read, so no other root's were touched.
        reconciled_at: The instant the pass judged every lease at.
        examined: How many leases the root holds.
        expired: How many active leases the deadline caught.
        released: How many were cleaned because their residue was shown
            to be worthless.
        quarantined: How many were left alone under a recovery handle.
        untouched: The lease ids the pass deliberately left as they were,
            sorted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    reconciled_at: UtcDatetime
    examined: Annotated[int, Field(ge=0)]
    expired: Annotated[int, Field(ge=0)]
    released: Annotated[int, Field(ge=0)]
    quarantined: Annotated[int, Field(ge=0)]
    untouched: tuple[str, ...] = ()


def lease_path(context: Epoch2RootContext, *, lease_id: str) -> Path:
    """Return the file one root files a lease in.

    Args:
        context: The native context of the root the lease belongs to.
        lease_id: The lease's identifier.

    Returns:
        The declared path, whether or not it exists.

    Raises:
        UndeclaredPathError: The commit policy declares no row for the
            lease family.
    """
    return context.declared_path(context.identity.tree_root / LEASE_LOCATOR / f"{lease_id}.json")


def workspace_path(context: Epoch2RootContext, *, handle: str) -> Path:
    """Resolve an opaque workspace handle to the directory it names.

    This is the only resolver. A worker holds the handle and never the
    answer, so a worker cannot address a tree the daemon did not give it.

    Args:
        context: The native context of the root the workspace is under.
        handle: The opaque handle.

    Returns:
        The declared workspace directory, whether or not it exists.

    Raises:
        UndeclaredPathError: The commit policy declares no row for the
            workspace family.
    """
    return context.declared_path(context.identity.tree_root / WORKSPACE_LOCATOR / handle)


def quarantine_path(context: Epoch2RootContext, *, recovery_handle: str) -> Path:
    """Return the file one root records quarantined residue in.

    Args:
        context: The native context of the root.
        recovery_handle: The handle the residue is recovered by.

    Returns:
        The declared path of the quarantine record.

    Raises:
        UndeclaredPathError: The commit policy declares no row for the
            quarantine family.
    """
    return context.declared_path(
        context.identity.tree_root / QUARANTINE_LOCATOR / f"{recovery_handle}.json"
    )


def read_lease(context: Epoch2RootContext, *, lease_id: str) -> WorkLease | None:
    """Return the lease filed under *lease_id*, if one is.

    Args:
        context: The native context of the root.
        lease_id: The lease's identifier.

    Returns:
        The stored lease, or ``None`` when this root holds none.

    Raises:
        ValueError: The file exists but does not decode, does not
            validate, or holds another lease's id. Every case means the
            store cannot say what the lease is, and answering "none"
            would issue a second one over the same worktree.
    """
    path = lease_path(context, lease_id=lease_id)
    if not path.exists():
        return None
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as error:
        raise ValueError(f"the lease filed under {lease_id!r} is not valid JSON") from error
    lease = WorkLease.model_validate(payload)
    if lease.lease_id != lease_id:
        raise ValueError(f"the lease file of {lease_id!r} holds a lease filed under another id")
    return lease


def write_lease(context: Epoch2RootContext, lease: WorkLease) -> Path:
    """Persist *lease* into its root's store.

    Args:
        context: The native context of the root.
        lease: The lease to file.

    Returns:
        The path written.

    Raises:
        UndeclaredPathError: The commit policy declares no row for it.
    """
    path = lease_path(context, lease_id=lease.lease_id)
    atomic_write_json(path, lease.model_dump(mode="json"))
    logger.info(
        f"write_lease root={context.identity.root_id} lease={lease.lease_id} "
        f"status={lease.status.value}"
    )
    return path


def root_leases(context: Epoch2RootContext) -> tuple[WorkLease, ...]:
    """Return every lease this root holds, sorted by identifier.

    Args:
        context: The native context of the root.

    Returns:
        The stored leases. An unreadable file is skipped and logged
        rather than raised: a sweep that aborts on one bad record leaves
        every later root's residue unreconciled.
    """
    directory = context.identity.tree_root / LEASE_LOCATOR
    if not directory.is_dir():
        return ()
    leases: list[WorkLease] = []
    for path in sorted(directory.glob("*.json")):
        try:
            leases.append(WorkLease.model_validate(orjson.loads(path.read_bytes())))
        except orjson.JSONDecodeError, ValueError:
            logger.warning(f"root_leases skipped unreadable lease file={path.name}")
    return tuple(leases)


def issue_lease(
    context: Epoch2RootContext,
    *,
    run_ref: str,
    task_ref: str,
    purpose: RunPurpose,
    base: str,
    writable_roots: tuple[str, ...],
    now: datetime,
    ttl: timedelta,
) -> WorkLease:
    """Materialize an isolated worktree and hand back the lease over it.

    The lease is filed at every step of the walk, so a crash between two
    of them leaves a record a later reconcile can find rather than a
    directory nothing points at.

    Args:
        context: The native context of the root. A tree that is not a
            canary is refused by the session before anything is written.
        run_ref: The Run the lease is issued to.
        task_ref: The Task whose workspace it is.
        purpose: Why the Run writes.
        base: The ref the worktree is materialized at.
        writable_roots: The repository-relative roots a write may land
            under.
        now: When the lease is issued.
        ttl: How long past *now* it stays valid.

    Returns:
        The ``ACTIVE`` lease. Its ``workspace_handle`` is what a worker
        is told; the path it resolves to is not.

    Raises:
        LeaseRefusedError: The purpose writes nothing, or this Run
            already holds an active lease.
        UserError: The base ref does not resolve, or git is absent.
        StateConflict: ``git worktree add`` failed; the lease is left
            ``FAILED`` rather than removed.
        NativeAuthorityRequiredError: The tree is not in epoch 2.
    """
    if purpose not in MUTATING_PURPOSES:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.PURPOSE_NOT_MUTATING,
            detail=f"{purpose.value} writes nothing, so it is issued no workspace",
        )
    run_urn = parse_qualified_urn(run_ref)
    task_urn = parse_qualified_urn(task_ref)
    repo_root = context.identity.tree_root.parent
    base_commit = git.commit_sha(repo_root, base)
    with context.session([run_urn]):
        _refuse_second_active_lease(context, run_ref=run_ref, now=now)
        handle = f"wsh-{secrets.token_hex(16)}"
        lease = WorkLease(
            schema_version=LEASE_SCHEMA_VERSION,
            lease_id=f"LSE-{secrets.token_hex(16)}",
            run_ref=run_urn,
            task_ref=task_urn,
            purpose=purpose,
            workspace_handle=handle,
            workspace_generation=next_workspace_generation(
                held.workspace_generation
                for held in root_leases(context)
                if canonical_entity_urn(held.task_ref) == canonical_entity_urn(task_ref)
            ),
            branch=f"{LEASE_BRANCH_PREFIX}/{handle}",
            base_commit=base_commit,
            writable_roots=writable_roots,
            issued_at=now,
            heartbeat_at=now,
            expires_at=now + ttl,
            status_at=now,
            status=INITIAL_LEASE_STATUS,
        )
        write_lease(context, lease)
        lease = apply_lease_transition(lease, to=LeaseStatus.MATERIALIZING, at=now)
        write_lease(context, lease)
        target = workspace_path(context, handle=handle)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            git.worktree_add(repo_root, branch=lease.branch, path=target, base=base_commit)
        except cli_errors.CliError, OSError:
            write_lease(context, apply_lease_transition(lease, to=LeaseStatus.FAILED, at=now))
            raise
        lease = apply_lease_transition(lease, to=LeaseStatus.ACTIVE, at=now)
        write_lease(context, lease)
    logger.info(
        f"issue_lease root={context.identity.root_id} lease={lease.lease_id} "
        f"generation={lease.workspace_generation}"
    )
    return lease


def require_active_lease(
    context: Epoch2RootContext, *, purpose: RunPurpose, lease_id: str | None, now: datetime
) -> WorkLease:
    """Return the active lease a mutating purpose writes under, or refuse.

    Args:
        context: The native context of the root.
        purpose: Why the Run intends to write.
        lease_id: The lease it claims to hold, or ``None``.
        now: The instant the claim is judged at.

    Returns:
        The lease, which is active and not past its deadline.

    Raises:
        LeaseRefusedError: The purpose writes nothing, no lease was
            named, the named lease is not this root's, it is in any
            status but active, or its deadline has passed. Every case
            leaves the store untouched.
        ValueError: The stored lease cannot be read.
    """
    if purpose not in MUTATING_PURPOSES:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.PURPOSE_NOT_MUTATING,
            detail=f"{purpose.value} writes nothing, so it holds no workspace lease",
        )
    if not lease_id:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_REQUIRED,
            detail=f"a {purpose.value} run writes only under a daemon-issued lease and named none",
        )
    lease = read_lease(context, lease_id=lease_id)
    if lease is None:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_NOT_FOUND,
            detail=f"this root has issued no lease {lease_id!r}",
        )
    if lease.status is not LeaseStatus.ACTIVE:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_NOT_ACTIVE,
            detail=f"lease {lease_id} is {lease.status.value}, so it grants no write",
        )
    if lease_has_expired(lease, now=now):
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_EXPIRED,
            detail=f"lease {lease_id} reached its deadline, which only the daemon moves",
        )
    return lease


def open_workspace(
    context: Epoch2RootContext,
    *,
    purpose: RunPurpose,
    lease_id: str | None,
    relative: str,
    now: datetime,
) -> Path:
    """Return the absolute path of *relative* inside a leased workspace.

    This is where a mutating purpose becomes a place to write. Without an
    active lease no path is produced at all, so a Run that never got one
    has nowhere to put a byte, and a path that leaves the leased tree is
    refused before it is joined to anything.

    Args:
        context: The native context of the root.
        purpose: Why the Run intends to write.
        lease_id: The lease it claims to hold.
        relative: The repository-relative path it wants.
        now: The instant the claim is judged at.

    Returns:
        The resolved path inside the leased worktree.

    Raises:
        LeaseRefusedError: The lease does not admit the write, or the
            path is not under one of the lease's writable roots, or it
            resolves outside the leased tree.
        ValueError: The stored lease cannot be read.
    """
    lease = require_active_lease(context, purpose=purpose, lease_id=lease_id, now=now)
    if not write_admitted(lease, relative=relative):
        roots = ", ".join(lease.writable_roots)
        raise LeaseRefusedError(
            code=LeaseRefusalCode.SCOPE_ESCAPE,
            detail=f"{relative!r} is not under a writable root of lease {lease.lease_id}: {roots}",
        )
    root = workspace_path(context, handle=lease.workspace_handle)
    resolved = root / relative
    if not is_path_inside(resolved, root=root):
        raise LeaseRefusedError(
            code=LeaseRefusalCode.SCOPE_ESCAPE,
            detail=f"{relative!r} resolves outside the workspace of lease {lease.lease_id}",
        )
    return resolved.resolve(strict=False)


def heartbeat_lease(
    context: Epoch2RootContext,
    *,
    lease_id: str,
    now: datetime,
    origin: HeartbeatOrigin,
    extend: timedelta | None = None,
) -> WorkLease:
    """Record one liveness beat, and move the deadline only for the daemon.

    An agent's beat is evidence that work is still running. It is never
    evidence that the work deserves longer, so a beat carrying a
    requested extension is refused outright rather than quietly accepted
    with the extension dropped: the caller asked for something it may not
    have, and finding that out is the point.

    Args:
        context: The native context of the root.
        lease_id: The lease being beaten.
        now: When the beat arrived.
        origin: Who sent it.
        extend: How much longer the lease should last. Daemon-only.

    Returns:
        The stored lease after the beat. On an agent-originated beat its
        ``expires_at`` reads exactly as it did before.

    Raises:
        LeaseAuthorityError: An agent-originated beat asked to extend the
            deadline. Nothing was written.
        LeaseRefusedError: The lease is not this root's, or is not
            active, or has already expired.
        LeaseTransitionError: The beat is stamped before the last one.
        ValueError: The stored lease cannot be read.
    """
    if origin is not HeartbeatOrigin.DAEMON and extend is not None:
        raise LeaseAuthorityError(
            f"a {origin.value}-originated heartbeat cannot extend lease {lease_id}: "
            "the deadline is the daemon's alone"
        )
    with context.session([_locked_run(context, lease_id=lease_id)]):
        lease = _require_stored(context, lease_id=lease_id)
        if lease.status is not LeaseStatus.ACTIVE:
            raise LeaseRefusedError(
                code=LeaseRefusalCode.LEASE_NOT_ACTIVE,
                detail=f"lease {lease_id} is {lease.status.value}, so no beat revives it",
            )
        if lease_has_expired(lease, now=now):
            raise LeaseRefusedError(
                code=LeaseRefusalCode.LEASE_EXPIRED,
                detail=f"lease {lease_id} expired; a beat does not reopen a closed deadline",
            )
        beaten = (
            renew_lease(lease, at=now, ttl=extend)
            if extend is not None
            else record_heartbeat(lease, at=now)
        )
        write_lease(context, beaten)
    logger.info(
        f"heartbeat_lease root={context.identity.root_id} lease={lease_id} origin={origin.value}"
    )
    return beaten


def revoke_lease(
    context: Epoch2RootContext, *, lease_id: str, now: datetime, hard: bool = True
) -> WorkLease:
    """Withdraw a lease, leaving its residue for the reconcile to judge.

    Args:
        context: The native context of the root.
        lease_id: The lease to withdraw.
        now: When the withdrawal was decided.
        hard: A hard revoke drops straight to ``REVOKED`` and waits for a
            reconcile. A soft one enters ``REVOKING``, which releases
            once the residue is known to be integrated or safely kept.

    Returns:
        The stored lease after the withdrawal.

    Raises:
        LeaseRefusedError: The lease is not this root's, or is not
            active.
        ValueError: The stored lease cannot be read.
    """
    target = LeaseStatus.REVOKED if hard else LeaseStatus.REVOKING
    with context.session([_locked_run(context, lease_id=lease_id)]):
        lease = _require_stored(context, lease_id=lease_id)
        if lease.status is not LeaseStatus.ACTIVE:
            raise LeaseRefusedError(
                code=LeaseRefusalCode.LEASE_NOT_ACTIVE,
                detail=f"lease {lease_id} is {lease.status.value}, so there is nothing to revoke",
            )
        revoked = apply_lease_transition(lease, to=target, at=now)
        write_lease(context, revoked)
    logger.info(f"revoke_lease root={context.identity.root_id} lease={lease_id} hard={hard}")
    return revoked


def reconcile_root(context: Epoch2RootContext, *, now: datetime) -> ReconcileReport:
    """Walk one root's leases, cleaning what is worthless and keeping the rest.

    Only this root's store is read, so leases of every other root are
    left exactly as they were, and no ``state.json`` is opened at all, so
    a production root's epoch-1 worktree ledger is untouched by
    construction rather than by care.

    Args:
        context: The native context of the root.
        now: The instant every deadline is judged against. Supplied by
            the caller so a pass is a function of its argument.

    Returns:
        What the pass found and did.

    Raises:
        NativeAuthorityRequiredError: The tree is not in epoch 2.
    """
    held = root_leases(context)
    expired = released = quarantined = 0
    untouched: list[str] = []
    for lease in held:
        with context.session([lease.run_ref]):
            # The listing above took no lock, so the record is re-read
            # here: another session may have moved it in between.
            current = read_lease(context, lease_id=lease.lease_id)
            if current is None:
                continue
            if current.status is LeaseStatus.ACTIVE and lease_has_expired(current, now=now):
                current = apply_lease_transition(current, to=LeaseStatus.EXPIRED, at=now)
                write_lease(context, current)
                expired += 1
            outcome = _reconcile_one(context, lease=current, now=now)
            if outcome is ReconcileOutcome.RELEASED:
                released += 1
            elif outcome is ReconcileOutcome.QUARANTINED:
                quarantined += 1
            else:
                untouched.append(current.lease_id)
    report = ReconcileReport(
        root_id=context.identity.root_id,
        reconciled_at=now,
        examined=len(held),
        expired=expired,
        released=released,
        quarantined=quarantined,
        untouched=tuple(sorted(untouched)),
    )
    logger.info(
        f"reconcile_root root={report.root_id} examined={report.examined} "
        f"released={report.released} quarantined={report.quarantined}"
    )
    return report


def _reconcile_one(
    context: Epoch2RootContext, *, lease: WorkLease, now: datetime
) -> ReconcileOutcome:
    """Judge one lease's residue and file the outcome.

    Returns:
        What the pass did to it. ``UNTOUCHED`` covers both a lease the
        pass has no business with -- a live one, a terminal one -- and a
        withdrawing one whose residue it could not yet prove safe, since
        in every such case the record comes out as it went in.
    """
    if lease.status is RELEASE_ONLY_STATUS:
        return _release_once_proven(context, lease=lease, now=now)
    if lease.status not in RECONCILABLE_STATUSES:
        return ReconcileOutcome.UNTOUCHED
    current = lease
    if current.status is not LeaseStatus.RECONCILING:
        current = apply_lease_transition(current, to=LeaseStatus.RECONCILING, at=now)
        write_lease(context, current)
    verdict = _inspect_residue(context, lease=current)
    if verdict.reason is None:
        return _release(context, lease=current, now=now)
    return _quarantine(context, lease=current, verdict=verdict, now=now)


def _release_once_proven(
    context: Epoch2RootContext, *, lease: WorkLease, now: datetime
) -> ReconcileOutcome:
    """Release a withdrawing lease, or leave it where it is.

    A withdrawing lease has exactly one exit and it is conditional on the
    residue being proven safe, so an unproven one is not quarantined --
    the machine declares no such edge -- but left for a later pass.
    """
    if _inspect_residue(context, lease=lease).reason is not None:
        logger.info(f"_release_once_proven retained lease={lease.lease_id}")
        return ReconcileOutcome.UNTOUCHED
    return _release(context, lease=lease, now=now)


def _release(context: Epoch2RootContext, *, lease: WorkLease, now: datetime) -> ReconcileOutcome:
    """Remove a worktree shown to hold nothing and end its lease."""
    _remove_workspace(context, lease=lease)
    write_lease(context, apply_lease_transition(lease, to=LeaseStatus.RELEASED, at=now))
    return ReconcileOutcome.RELEASED


def _quarantine(
    context: Epoch2RootContext, *, lease: WorkLease, verdict: _Residue, now: datetime
) -> ReconcileOutcome:
    """File a recovery handle over residue nobody could vouch for.

    Nothing on disk is moved or removed. The record is how the residue is
    found again; the residue itself stays exactly where the worker left
    it.
    """
    assert verdict.reason is not None, "a quarantine always states its reason"
    recovery_handle = f"rec-{secrets.token_hex(16)}"
    record = QuarantineRecord(
        schema_version=QUARANTINE_SCHEMA_VERSION,
        recovery_handle=recovery_handle,
        lease_id=lease.lease_id,
        workspace_handle=lease.workspace_handle,
        workspace_generation=lease.workspace_generation,
        branch=lease.branch,
        base_commit=lease.base_commit,
        reason=verdict.reason,
        residue=verdict.residue,
        workspace_present=verdict.present,
        quarantined_at=now,
    )
    atomic_write_json(
        quarantine_path(context, recovery_handle=recovery_handle),
        record.model_dump(mode="json"),
    )
    write_lease(
        context,
        apply_lease_transition(
            lease,
            to=LeaseStatus.QUARANTINED,
            at=now,
            quarantine_reason=verdict.reason,
            recovery_handle=recovery_handle,
        ),
    )
    logger.warning(f"_quarantine retained root={context.identity.root_id} lease={lease.lease_id}")
    return ReconcileOutcome.QUARANTINED


@dataclass(frozen=True, slots=True)
class _Residue:
    """What one leased worktree was observed to be holding.

    Attributes:
        reason: Why the residue could not be shown to be worthless, or
            ``None`` when it was.
        residue: A bounded sample of what git reported.
        present: Whether the directory was there to look at.
    """

    reason: str | None
    residue: tuple[str, ...]
    present: bool


def _inspect_residue(context: Epoch2RootContext, *, lease: WorkLease) -> _Residue:
    """Return whether a leased worktree holds anything anybody could want.

    Clean means two things at once: git reports no modified, staged or
    untracked entry, and HEAD still reads as the commit the worktree was
    materialized at. A worktree that committed its work has residue that
    is not in the integration base, which is exactly the content the
    reconcile must not remove.
    """
    target = workspace_path(context, handle=lease.workspace_handle)
    if not target.is_dir():
        return _Residue(
            reason="the leased workspace is gone, so what it held cannot be shown to be integrated",
            residue=(),
            present=False,
        )
    try:
        porcelain = tuple(git.status_porcelain(target))
        head = git.commit_sha(target, "HEAD")
    except (cli_errors.CliError, OSError) as error:
        logger.warning(
            f"_inspect_residue unreadable lease={lease.lease_id} error={type(error).__name__}"
        )
        return _Residue(
            reason="git cannot report the leased workspace, so its residue is unknown",
            residue=(),
            present=True,
        )
    if porcelain:
        return _Residue(
            reason=f"the leased workspace holds {len(porcelain)} uncommitted entries",
            residue=porcelain[:RESIDUE_SAMPLE_WIDTH],
            present=True,
        )
    if head != lease.base_commit:
        return _Residue(
            reason="the leased workspace committed work that is not in the base it started from",
            residue=(head,),
            present=True,
        )
    return _Residue(reason=None, residue=(), present=True)


def _remove_workspace(context: Epoch2RootContext, *, lease: WorkLease) -> None:
    """Remove a worktree already shown to hold nothing, and its branch.

    Failure is logged and swallowed. The residue was proven worthless
    before this ran, so a directory that outlives its lease costs disk
    and nothing else, while raising here would strand every later lease
    in the same pass.
    """
    repo_root = context.identity.tree_root.parent
    target = workspace_path(context, handle=lease.workspace_handle)
    try:
        git.worktree_remove(repo_root, path=target)
        git.branch_delete(repo_root, name=lease.branch)
    except (cli_errors.CliError, OSError) as error:
        logger.warning(
            f"_remove_workspace left lease={lease.lease_id} error={type(error).__name__}"
        )


def _refuse_second_active_lease(context: Epoch2RootContext, *, run_ref: str, now: datetime) -> None:
    """Refuse a Run that already holds a live lease.

    The check runs inside the session, so two concurrent issues of one
    Run serialize on the root's document lock and exactly one of them
    sees an empty store.

    Raises:
        LeaseRefusedError: The Run holds an active lease that has not
            reached its deadline.
    """
    wanted = canonical_entity_urn(run_ref)
    for held in root_leases(context):
        same_run = canonical_entity_urn(held.run_ref) == wanted
        if same_run and held.status is LeaseStatus.ACTIVE and not lease_has_expired(held, now=now):
            raise LeaseRefusedError(
                code=LeaseRefusalCode.LEASE_ALREADY_ACTIVE,
                detail=f"{run_ref} already holds lease {held.lease_id}; one active lease per run",
            )


def _locked_run(context: Epoch2RootContext, *, lease_id: str) -> str:
    """Return the Run URN a lease's session locks on.

    Reading the lease before the lock is safe: the URN is immutable for
    the life of the record, so a concurrent write cannot move which lock
    this session should have taken.

    Raises:
        LeaseRefusedError: This root has issued no such lease.
        ValueError: The stored lease cannot be read.
    """
    lease = read_lease(context, lease_id=lease_id)
    if lease is None:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_NOT_FOUND,
            detail=f"this root has issued no lease {lease_id!r}",
        )
    return str(lease.run_ref)


def _require_stored(context: Epoch2RootContext, *, lease_id: str) -> WorkLease:
    """Return the stored lease, refusing one this root never issued.

    Raises:
        LeaseRefusedError: This root has issued no such lease.
        ValueError: The stored lease cannot be read.
    """
    lease = read_lease(context, lease_id=lease_id)
    if lease is None:
        raise LeaseRefusedError(
            code=LeaseRefusalCode.LEASE_NOT_FOUND,
            detail=f"this root has issued no lease {lease_id!r}",
        )
    return lease


__all__ = [
    "LEASE_BRANCH_PREFIX",
    "LEASE_LOCATOR",
    "QUARANTINE_LOCATOR",
    "QUARANTINE_SCHEMA_VERSION",
    "RECONCILABLE_STATUSES",
    "RELEASE_ONLY_STATUS",
    "RESIDUE_SAMPLE_WIDTH",
    "WORKSPACE_LOCATOR",
    "LeaseRefusalCode",
    "LeaseRefusedError",
    "QuarantineRecord",
    "ReconcileOutcome",
    "ReconcileReport",
    "heartbeat_lease",
    "issue_lease",
    "lease_path",
    "open_workspace",
    "quarantine_path",
    "read_lease",
    "reconcile_root",
    "require_active_lease",
    "revoke_lease",
    "root_leases",
    "workspace_path",
    "write_lease",
]
