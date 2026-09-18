"""The lease one mutating Run writes under, and the edges it may take.

A mutating Run does not own a worktree; it borrows one. The borrowing is
a :class:`WorkLease`, and everything about it is daemon-owned: who issued
it, which tree it points at, and -- the field that matters most -- when
it stops being valid. An agent holding a lease can prove it is alive, and
that is all it can do to it. It cannot move ``expires_at``, because no
function here that an agent's request can reach takes an expiry at all:
:func:`apply_lease_transition` has no expiry parameter and
:func:`record_heartbeat` moves only the liveness stamp. Extending a lease
is :func:`renew_lease`, which the daemon calls on its own authority.

The worker is handed ``workspace_handle`` and never a path. A handle
carries no directory anywhere in its grammar, so a worker cannot derive
the tree's location from it, cannot address a sibling tree by editing it,
and cannot act outside the tree the daemon resolved it to. The daemon
resolves a handle to a path; nothing else can.

``workspace_generation`` counts rematerializations of one Task's
workspace and only ever rises: a candidate sealed against generation 2 is
not a candidate for the tree generation 3 replaced it with, so a stale
generation is detectable rather than plausible.

The status machine has one entry, ``REQUESTED``, and three exits. An
uncertain exit is ``QUARANTINED``, which is not a tidier spelling of
deleted: it is the state that says residue was left alone and names the
handle that recovers it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from eawf.kernel.runtime.provider import RuntimeRecord
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose, WriteSetPath
from eawf.kernel.state.epoch2.urns import RunUrn, TaskUrn
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime

#: Version of the persisted lease record shape.
LEASE_SCHEMA_VERSION: Final = "1"

#: A lease's own identifier. Random rather than derived, so nothing about
#: the Run or the tree can be read out of it.
LeaseId = Annotated[str, StringConstraints(strict=True, pattern=r"^LSE-[0-9a-f]{32}$")]

#: The opaque name a worker knows its workspace by. The grammar admits no
#: separator, so a handle cannot spell a path, a parent directory, or a
#: sibling tree however it is concatenated.
WorkspaceHandle = Annotated[str, StringConstraints(strict=True, pattern=r"^wsh-[0-9a-f]{32}$")]

#: The name quarantined residue is recovered by. Same shape rule as a
#: workspace handle and for the same reason.
RecoveryHandle = Annotated[str, StringConstraints(strict=True, pattern=r"^rec-[0-9a-f]{32}$")]

#: Why residue was quarantined, in one bounded operator-facing line.
QuarantineReason = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)
]


class LeaseStatus(StrEnum):
    """Where one lease sits in its machine."""

    REQUESTED = "requested"
    MATERIALIZING = "materializing"
    ACTIVE = "active"
    REVOKING = "revoking"
    EXPIRED = "expired"
    REVOKED = "revoked"
    RECONCILING = "reconciling"
    RELEASED = "released"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class HeartbeatOrigin(StrEnum):
    """Who sent a heartbeat, which decides what it is allowed to move.

    An agent-originated beat is evidence of liveness and nothing else. A
    daemon-originated beat is the only one that may carry a renewal,
    because the daemon is the only party whose clock the lease trusts.
    """

    DAEMON = "daemon"
    AGENT = "agent"


#: Every edge the lease machine declares, as successor sets. A status
#: absent from a predecessor's set is not reachable from it: the machine
#: is the table, not a convention the callers share.
LEASE_EDGES: Final[Mapping[LeaseStatus, frozenset[LeaseStatus]]] = {
    LeaseStatus.REQUESTED: frozenset({LeaseStatus.MATERIALIZING}),
    LeaseStatus.MATERIALIZING: frozenset({LeaseStatus.ACTIVE, LeaseStatus.FAILED}),
    LeaseStatus.ACTIVE: frozenset(
        {
            LeaseStatus.REVOKING,
            LeaseStatus.EXPIRED,
            LeaseStatus.REVOKED,
            LeaseStatus.QUARANTINED,
        }
    ),
    LeaseStatus.REVOKING: frozenset({LeaseStatus.RELEASED}),
    LeaseStatus.EXPIRED: frozenset({LeaseStatus.RECONCILING}),
    LeaseStatus.REVOKED: frozenset({LeaseStatus.RECONCILING}),
    LeaseStatus.RECONCILING: frozenset({LeaseStatus.RELEASED, LeaseStatus.QUARANTINED}),
    LeaseStatus.RELEASED: frozenset(),
    LeaseStatus.QUARANTINED: frozenset(),
    LeaseStatus.FAILED: frozenset(),
}

#: The one status a lease is created in. A record that started anywhere
#: else would have skipped the admission the machine exists to gate.
INITIAL_LEASE_STATUS: Final = LeaseStatus.REQUESTED

#: The statuses a lease never leaves.
TERMINAL_LEASE_STATUSES: Final[frozenset[LeaseStatus]] = frozenset(
    status for status, successors in LEASE_EDGES.items() if not successors
)


class LeaseTransitionError(ValueError):
    """A lease was asked to take an edge its machine does not declare."""


class LeaseAuthorityError(ValueError):
    """Something other than the daemon asked to move a lease's expiry."""


class WorkLease(RuntimeRecord):
    """One daemon-issued borrow of an isolated worktree.

    Attributes:
        schema_version: Version of this record's shape.
        lease_id: The lease's own identifier.
        run_ref: The Run the lease was issued to. Exactly one lease of a
            Run is ever active.
        task_ref: The Task whose workspace this is.
        purpose: Why the Run writes. Only a mutating purpose holds a
            lease at all.
        workspace_handle: The opaque name the worker knows the workspace
            by. It spells no path and resolves only daemon-side.
        workspace_generation: Which materialization of this Task's
            workspace the handle points at. Rises on every rematerialize.
        branch: The branch the worktree was materialized on.
        base_commit: The commit the worktree was materialized at, so a
            candidate can be checked against what it started from.
        writable_roots: The repository-relative roots a write may land
            under. At least one, since a lease granting no write grants
            nothing.
        issued_at: When the daemon created the lease.
        heartbeat_at: The last liveness stamp, from either origin.
        expires_at: When the lease stops being valid. Daemon-owned.
        status_at: When the lease entered its current status.
        status: Where it sits in the machine.
        quarantine_reason: Why residue was left alone, on a quarantined
            lease and on no other.
        recovery_handle: How that residue is found again.
    """

    schema_version: Literal["1"] = LEASE_SCHEMA_VERSION
    lease_id: LeaseId
    run_ref: RunUrn
    task_ref: TaskUrn
    purpose: RunPurpose
    workspace_handle: WorkspaceHandle
    workspace_generation: StrictPositiveInt
    branch: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
    base_commit: ShaStr
    writable_roots: Annotated[tuple[WriteSetPath, ...], Field(min_length=1)]
    issued_at: UtcDatetime
    heartbeat_at: UtcDatetime
    expires_at: UtcDatetime
    status_at: UtcDatetime
    status: LeaseStatus
    quarantine_reason: QuarantineReason | None = None
    recovery_handle: RecoveryHandle | None = None

    @model_validator(mode="after")
    def _purpose_is_a_writing_one(self) -> Self:
        """Refuse a lease issued to a Run that has nothing to write.

        Raises:
            ValueError: The purpose is read-only, so a workspace lease
                would grant an authority the Run's scope does not carry.
        """
        if self.purpose not in MUTATING_PURPOSES:
            admitted = ", ".join(sorted(purpose.value for purpose in MUTATING_PURPOSES))
            raise ValueError(
                f"{self.purpose.value} writes nothing; a lease needs one of {admitted}"
            )
        return self

    @model_validator(mode="after")
    def _stamps_do_not_precede_issue(self) -> Self:
        """Refuse a lease whose clock reads backwards.

        Raises:
            ValueError: A stamp predates the issue, or the lease expires
                at or before it was issued, which is a lease that was
                never valid for any instant.
        """
        if self.heartbeat_at < self.issued_at:
            raise ValueError("heartbeat_at precedes issued_at")
        if self.status_at < self.issued_at:
            raise ValueError("status_at precedes issued_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at is not after issued_at")
        return self

    @model_validator(mode="after")
    def _quarantine_names_its_recovery(self) -> Self:
        """Tie the quarantine fields to the quarantine status, both ways.

        Raises:
            ValueError: A quarantined lease omits its reason or its
                recovery handle, which would leave residue nobody can
                find, or a lease that is not quarantined carries one,
                which would claim residue nobody set aside.
        """
        quarantined = self.status is LeaseStatus.QUARANTINED
        present = self.quarantine_reason is not None and self.recovery_handle is not None
        if quarantined and not present:
            raise ValueError("a quarantined lease states its reason and its recovery handle")
        if not quarantined and (
            self.quarantine_reason is not None or self.recovery_handle is not None
        ):
            raise ValueError(f"a {self.status.value} lease carries no quarantine fields")
        return self


def lease_edge_admitted(current: LeaseStatus, target: LeaseStatus) -> bool:
    """Return whether the machine declares the edge *current* to *target*.

    Args:
        current: The status the lease sits in.
        target: The status it is asked to move to.

    Returns:
        ``True`` when the edge is declared. A terminal status admits
        nothing, and no status admits itself: a lease that did not move
        took no edge.
    """
    return target in LEASE_EDGES[current]


def lease_has_expired(lease: WorkLease, *, now: datetime) -> bool:
    """Return whether *lease* has reached its deadline by *now*.

    The comparison is inclusive: a lease is dead at the instant it
    expires, not one tick after, so two observers reading the same clock
    never disagree about it.

    Args:
        lease: The lease to judge.
        now: The instant to judge it at, supplied by the caller so the
            answer is a function of its argument and not of the wall
            clock.

    Returns:
        ``True`` when the deadline has arrived.
    """
    return now >= lease.expires_at


def write_admitted(lease: WorkLease, *, relative: str) -> bool:
    """Return whether *relative* lies under one of the lease's roots.

    The check is on the repository-relative spelling alone, before any
    path is joined, so a request that escapes the tree is refused without
    the escape ever being resolved against a real directory.

    Args:
        lease: The lease whose writable roots bound the write.
        relative: The repository-relative path the worker named.

    Returns:
        ``True`` when the path is contained by a declared root. An
        absolute path, a backslash-flavoured one, an empty one, and any
        path with a ``..`` or ``.`` segment are all refused, because none
        of them names one place inside the tree.
    """
    if not relative or relative.startswith("/") or "\\" in relative:
        return False
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return any(
        parts[: len(root_parts)] == root_parts
        for root_parts in (root.split("/") for root in lease.writable_roots)
    )


def next_workspace_generation(generations: Iterable[int]) -> int:
    """Return the generation the next materialization of a workspace takes.

    Args:
        generations: Every generation already issued for one Task, in any
            order. An empty run yields the first generation.

    Returns:
        One past the highest generation seen, so the sequence rises even
        when the leases between were released or quarantined.

    Raises:
        ValueError: A generation is not a positive integer, so the
            sequence it belongs to is not one this counter produced.
    """
    highest = 0
    for generation in generations:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError(f"{generation!r} is not a workspace generation")
        highest = max(highest, generation)
    return highest + 1


def apply_lease_transition(
    lease: WorkLease,
    *,
    to: LeaseStatus,
    at: datetime,
    quarantine_reason: str | None = None,
    recovery_handle: str | None = None,
) -> WorkLease:
    """Return *lease* moved to *to*, or refuse the move having built nothing.

    The function takes no expiry. That is the whole of why an agent
    cannot extend a lease through a transition: there is no argument for
    it to supply and no branch that derives one.

    Args:
        lease: The lease to move.
        to: The status to move it to.
        at: When the move happened.
        quarantine_reason: Why residue was left alone. Required moving
            to ``QUARANTINED`` and refused otherwise.
        recovery_handle: How that residue is found again. Same rule.

    Returns:
        The successor record, re-validated through the lease model.

    Raises:
        LeaseTransitionError: The machine declares no such edge, or the
            move is stamped before the status it is leaving.
        pydantic.ValidationError: The successor breaks a lease rule, such
            as a quarantine without its recovery handle.
    """
    if not lease_edge_admitted(lease.status, to):
        successors = sorted(status.value for status in LEASE_EDGES[lease.status])
        admitted = ", ".join(successors) if successors else "nothing: it is terminal"
        raise LeaseTransitionError(
            f"lease {lease.lease_id} is {lease.status.value} and admits {admitted}, not {to.value}"
        )
    if at < lease.status_at:
        raise LeaseTransitionError(
            f"lease {lease.lease_id} entered {lease.status.value} after the move that leaves it"
        )
    return _revalidated(
        lease,
        status=to.value,
        status_at=at,
        quarantine_reason=quarantine_reason,
        recovery_handle=recovery_handle,
    )


def record_heartbeat(lease: WorkLease, *, at: datetime) -> WorkLease:
    """Return *lease* with its liveness stamp moved and its expiry untouched.

    Args:
        lease: The lease being proven alive.
        at: When the beat arrived.

    Returns:
        The successor record. ``expires_at`` reads exactly as it did,
        whoever sent the beat.

    Raises:
        LeaseTransitionError: The lease is not active, so there is no
            running work for a beat to be evidence of, or the beat is
            stamped before the last one.
    """
    _require_active(lease, verb="heartbeat")
    if at < lease.heartbeat_at:
        raise LeaseTransitionError(
            f"lease {lease.lease_id} was last beaten after the beat now offered"
        )
    return _revalidated(lease, heartbeat_at=at)


def renew_lease(lease: WorkLease, *, at: datetime, ttl: timedelta) -> WorkLease:
    """Return *lease* with a new deadline, on the daemon's authority alone.

    This is the only function that moves ``expires_at``. Callers reached
    by an agent request must check the origin before calling it.

    Args:
        lease: The lease being extended.
        at: When the renewal was decided, which is also the new liveness
            stamp.
        ttl: How long past *at* the lease stays valid.

    Returns:
        The successor record, expiring ``at + ttl``.

    Raises:
        LeaseTransitionError: The lease is not active, or the renewal
            would land at or before the instant it was decided, which is
            a lease that is already dead when it is granted.
    """
    _require_active(lease, verb="renew")
    if ttl <= timedelta(0):
        raise LeaseTransitionError(f"a renewal of lease {lease.lease_id} lasts a positive time")
    return _revalidated(lease, heartbeat_at=at, expires_at=at + ttl)


def _require_active(lease: WorkLease, *, verb: str) -> None:
    """Refuse *verb* on a lease that is not running.

    Raises:
        LeaseTransitionError: The lease is in any status but ``ACTIVE``.
    """
    if lease.status is not LeaseStatus.ACTIVE:
        raise LeaseTransitionError(
            f"lease {lease.lease_id} is {lease.status.value}, so it cannot {verb}"
        )


def _revalidated(lease: WorkLease, **changes: Any) -> WorkLease:
    """Return *lease* with *changes* applied and every rule re-checked.

    ``model_copy`` is deliberately not used: it skips the validators, and
    the rules that tie quarantine to its handle and the stamps to the
    issue are exactly what a successor must still satisfy.
    """
    payload = lease.model_dump(mode="json")
    for field, value in changes.items():
        payload[field] = value.isoformat() if isinstance(value, datetime) else value
    return WorkLease.model_validate(payload)


__all__ = [
    "INITIAL_LEASE_STATUS",
    "LEASE_EDGES",
    "LEASE_SCHEMA_VERSION",
    "TERMINAL_LEASE_STATUSES",
    "HeartbeatOrigin",
    "LeaseAuthorityError",
    "LeaseId",
    "LeaseStatus",
    "LeaseTransitionError",
    "QuarantineReason",
    "RecoveryHandle",
    "WorkLease",
    "WorkspaceHandle",
    "apply_lease_transition",
    "lease_edge_admitted",
    "lease_has_expired",
    "next_workspace_generation",
    "record_heartbeat",
    "renew_lease",
    "write_admitted",
]
