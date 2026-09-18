"""The lease machine admits its declared edges and nothing else.

The edge assertions are exhaustive rather than illustrative: every one of
the hundred ordered status pairs is driven through
:func:`apply_lease_transition`, and a pair the table does not declare has
to raise. A test that walked only the happy path would pass against a
machine that admitted everything, which is the failure the machine exists
to prevent.

The handle and generation assertions guard the two properties a worker
can observe. A handle must stay opaque -- no separator, no parent
segment, nothing a path could be built out of -- and a generation must
only ever rise, so a candidate sealed against one materialization is
never mistaken for a candidate against the next.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.lease import (
    INITIAL_LEASE_STATUS,
    LEASE_EDGES,
    LEASE_SCHEMA_VERSION,
    TERMINAL_LEASE_STATUSES,
    LeaseStatus,
    LeaseTransitionError,
    WorkLease,
    apply_lease_transition,
    lease_edge_admitted,
    lease_has_expired,
    next_workspace_generation,
    record_heartbeat,
    renew_lease,
    write_admitted,
)
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose

pytestmark = pytest.mark.unit


AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
TTL = timedelta(minutes=30)
HANDLE = "wsh-" + "a" * 32
RECOVERY = "rec-" + "b" * 32
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BASE = "c" * 40


def lease_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid lease payload with *overrides* applied."""
    payload: dict[str, Any] = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "lease_id": "LSE-" + "d" * 32,
        "run_ref": RUN_URN,
        "task_ref": TASK_URN,
        "purpose": RunPurpose.IMPLEMENT.value,
        "workspace_handle": HANDLE,
        "workspace_generation": 1,
        "branch": f"lease/{HANDLE}",
        "base_commit": BASE,
        "writable_roots": ["src/eawf"],
        "issued_at": AT.isoformat(),
        "heartbeat_at": AT.isoformat(),
        "expires_at": (AT + TTL).isoformat(),
        "status_at": AT.isoformat(),
        "status": INITIAL_LEASE_STATUS.value,
    }
    payload.update(overrides)
    return payload


def lease_in(status: LeaseStatus, **overrides: Any) -> WorkLease:
    """Return a valid lease sitting in *status*."""
    quarantine: dict[str, Any] = (
        {"quarantine_reason": "residue could not be vouched for", "recovery_handle": RECOVERY}
        if status is LeaseStatus.QUARANTINED
        else {}
    )
    return WorkLease.model_validate(lease_payload(status=status.value, **quarantine, **overrides))


# ---- the declared walk -------------------------------------------------------


def test_apply_lease_transition_walks_requested_to_materializing_to_active() -> None:
    """The one admission path a worker's workspace comes into being through."""
    lease = lease_in(LeaseStatus.REQUESTED)
    assert lease.status is LeaseStatus.REQUESTED
    materializing = apply_lease_transition(lease, to=LeaseStatus.MATERIALIZING, at=AT)
    assert materializing.status is LeaseStatus.MATERIALIZING
    active = apply_lease_transition(materializing, to=LeaseStatus.ACTIVE, at=AT)
    assert active.status is LeaseStatus.ACTIVE


def test_initial_status_is_the_only_one_without_a_predecessor() -> None:
    """Nothing reaches ``REQUESTED``, so a lease cannot start anywhere else."""
    reachable = {target for targets in LEASE_EDGES.values() for target in targets}
    assert INITIAL_LEASE_STATUS not in reachable
    assert set(LeaseStatus) - reachable == {INITIAL_LEASE_STATUS}


def test_terminal_statuses_are_exactly_released_quarantined_and_failed() -> None:
    """The three ends the spec declares, and no fourth."""
    expected = {LeaseStatus.RELEASED, LeaseStatus.QUARANTINED, LeaseStatus.FAILED}
    assert expected == TERMINAL_LEASE_STATUSES


@pytest.mark.parametrize("terminal", sorted(TERMINAL_LEASE_STATUSES))
def test_terminal_lease_admits_no_successor(terminal: LeaseStatus) -> None:
    """A finished lease cannot be reopened into any status at all."""
    lease = lease_in(terminal)
    for target in LeaseStatus:
        with pytest.raises(LeaseTransitionError, match="terminal"):
            apply_lease_transition(lease, to=target, at=AT)


@pytest.mark.parametrize(
    ("current", "target"), list(itertools.product(sorted(LeaseStatus), sorted(LeaseStatus)))
)
def test_every_status_pair_agrees_with_the_declared_table(
    current: LeaseStatus, target: LeaseStatus
) -> None:
    """Exhaustive: the mover admits a pair exactly when the table does."""
    lease = lease_in(current)
    declared = lease_edge_admitted(current, target)
    if not declared:
        with pytest.raises(LeaseTransitionError):
            apply_lease_transition(lease, to=target, at=AT)
        return
    quarantine: dict[str, Any] = (
        {"quarantine_reason": "residue is uncertain", "recovery_handle": RECOVERY}
        if target is LeaseStatus.QUARANTINED
        else {}
    )
    assert apply_lease_transition(lease, to=target, at=AT, **quarantine).status is target


def test_no_status_admits_itself() -> None:
    """A lease that did not move took no edge, so self-loops are refused."""
    for status in LeaseStatus:
        assert not lease_edge_admitted(status, status)


def test_transition_stamped_before_the_status_it_leaves_is_refused() -> None:
    """A clock that runs backwards is a defect, not an ordering to honour."""
    lease = lease_in(LeaseStatus.REQUESTED)
    with pytest.raises(LeaseTransitionError, match="after the move"):
        apply_lease_transition(lease, to=LeaseStatus.MATERIALIZING, at=AT - timedelta(seconds=1))


def test_transition_at_the_same_instant_is_admitted() -> None:
    """The off-by-one boundary: a same-instant move is not a backwards one."""
    lease = lease_in(LeaseStatus.REQUESTED)
    assert apply_lease_transition(lease, to=LeaseStatus.MATERIALIZING, at=AT).status_at == AT


def test_apply_lease_transition_has_no_expiry_parameter() -> None:
    """The structural reason no transition can extend a lease."""
    with pytest.raises(TypeError, match="expires_at"):
        apply_lease_transition(  # type: ignore[call-arg]
            lease_in(LeaseStatus.REQUESTED),
            to=LeaseStatus.MATERIALIZING,
            at=AT,
            expires_at=AT + timedelta(days=1),
        )


# ---- the opaque handle -------------------------------------------------------


@pytest.mark.parametrize(
    "handle",
    [
        "",
        "wsh-",
        "wsh-" + "a" * 31,
        "wsh-" + "a" * 33,
        "wsh-" + "A" * 32,
        "wsh-../" + "a" * 29,
        "/tmp/workspace",
        ".ea/worktrees/p33-w61",
        "wsh-" + "a" * 32 + "/src",
    ],
)
def test_workspace_handle_refuses_anything_path_shaped(handle: str) -> None:
    """A handle a path could be built out of is not opaque."""
    with pytest.raises(ValidationError):
        WorkLease.model_validate(lease_payload(workspace_handle=handle))


def test_workspace_handle_carries_no_separator() -> None:
    """The accepted handle holds nothing a filesystem would resolve."""
    lease = lease_in(LeaseStatus.ACTIVE)
    assert "/" not in lease.workspace_handle
    assert "\\" not in lease.workspace_handle
    assert ".." not in lease.workspace_handle


def test_lease_record_states_no_filesystem_path() -> None:
    """No field of the worker-facing record is a directory."""
    assert "path" not in set(WorkLease.model_fields)
    assert "workspace_path" not in set(WorkLease.model_fields)


# ---- the monotonic generation ------------------------------------------------


def test_next_workspace_generation_of_nothing_is_the_first() -> None:
    """The empty boundary: a Task materialized for the first time."""
    assert next_workspace_generation(()) == 1


def test_next_workspace_generation_of_one_is_the_second() -> None:
    """The single boundary."""
    assert next_workspace_generation((1,)) == 2


def test_next_workspace_generation_rises_past_the_highest_seen() -> None:
    """A gap left by a released lease does not lower the next generation."""
    assert next_workspace_generation((3, 1)) == 4


def test_next_workspace_generation_ignores_a_repeat() -> None:
    """Two leases recorded at one generation still yield one successor."""
    assert next_workspace_generation((2, 2)) == 3


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "2"])
def test_next_workspace_generation_refuses_a_non_generation(bad: object) -> None:
    """A member outside the positive integers is not from this sequence."""
    with pytest.raises(ValueError, match="not a workspace generation"):
        next_workspace_generation([bad])  # type: ignore[list-item]


@pytest.mark.parametrize("generation", [0, -1, 1.5, "1"])
def test_lease_refuses_a_non_positive_generation(generation: object) -> None:
    """A generation is a count of materializations, so it starts at one."""
    with pytest.raises(ValidationError):
        WorkLease.model_validate(lease_payload(workspace_generation=generation))


# ---- the record's own rules --------------------------------------------------


@pytest.mark.parametrize(
    "purpose", sorted(set(RunPurpose) - MUTATING_PURPOSES, key=lambda item: item.value)
)
def test_lease_refuses_a_read_only_purpose(purpose: RunPurpose) -> None:
    """A run that writes nothing is issued no workspace."""
    with pytest.raises(ValidationError, match="writes nothing"):
        WorkLease.model_validate(lease_payload(purpose=purpose.value))


def test_lease_refuses_an_unknown_key() -> None:
    """The record forbids extras like every other strict model."""
    with pytest.raises(ValidationError):
        WorkLease.model_validate(lease_payload(writable=True))


def test_lease_refuses_an_empty_writable_root_set() -> None:
    """A lease granting no write grants nothing, so it is not a lease."""
    with pytest.raises(ValidationError):
        WorkLease.model_validate(lease_payload(writable_roots=[]))


def test_lease_refuses_an_expiry_at_its_issue() -> None:
    """A lease valid for no instant was never valid."""
    with pytest.raises(ValidationError, match="not after issued_at"):
        WorkLease.model_validate(lease_payload(expires_at=AT.isoformat()))


def test_lease_refuses_a_heartbeat_before_its_issue() -> None:
    """A beat that predates the lease is not a beat of this lease."""
    with pytest.raises(ValidationError, match="precedes issued_at"):
        WorkLease.model_validate(
            lease_payload(heartbeat_at=(AT - timedelta(seconds=1)).isoformat())
        )


def test_quarantined_lease_without_a_recovery_handle_is_refused() -> None:
    """Residue set aside with no way to find it is residue thrown away."""
    with pytest.raises(ValidationError, match="recovery handle"):
        WorkLease.model_validate(
            lease_payload(status=LeaseStatus.QUARANTINED.value, quarantine_reason="uncertain")
        )


def test_unquarantined_lease_carrying_a_recovery_handle_is_refused() -> None:
    """A handle on a released lease claims residue nobody set aside."""
    with pytest.raises(ValidationError, match="no quarantine fields"):
        WorkLease.model_validate(
            lease_payload(status=LeaseStatus.RELEASED.value, recovery_handle=RECOVERY)
        )


def test_quarantine_transition_carries_both_fields_through() -> None:
    """The quarantined lease names its reason and the handle that finds it."""
    reconciling = lease_in(LeaseStatus.RECONCILING)
    quarantined = apply_lease_transition(
        reconciling,
        to=LeaseStatus.QUARANTINED,
        at=AT,
        quarantine_reason="the workspace holds uncommitted entries",
        recovery_handle=RECOVERY,
    )
    assert quarantined.recovery_handle == RECOVERY
    assert quarantined.quarantine_reason == "the workspace holds uncommitted entries"


# ---- liveness against expiry -------------------------------------------------


def test_record_heartbeat_moves_liveness_and_leaves_the_deadline() -> None:
    """A beat is evidence of running, never of deserving longer."""
    lease = lease_in(LeaseStatus.ACTIVE)
    beaten = record_heartbeat(lease, at=AT + timedelta(minutes=5))
    assert beaten.heartbeat_at == AT + timedelta(minutes=5)
    assert beaten.expires_at == lease.expires_at


def test_record_heartbeat_refuses_a_lease_that_is_not_active() -> None:
    """There is no running work for a beat of a released lease to prove."""
    with pytest.raises(LeaseTransitionError, match="cannot heartbeat"):
        record_heartbeat(lease_in(LeaseStatus.RELEASED), at=AT)


def test_record_heartbeat_refuses_a_beat_before_the_last_one() -> None:
    """Two beats out of order mean one of the clocks is wrong."""
    lease = lease_in(LeaseStatus.ACTIVE, heartbeat_at=(AT + timedelta(minutes=5)).isoformat())
    with pytest.raises(LeaseTransitionError, match="after the beat"):
        record_heartbeat(lease, at=AT)


def test_renew_lease_is_the_one_mover_of_the_deadline() -> None:
    """The daemon's own call, and the only path ``expires_at`` moves on."""
    lease = lease_in(LeaseStatus.ACTIVE)
    renewed = renew_lease(lease, at=AT + timedelta(minutes=10), ttl=TTL)
    assert renewed.expires_at == AT + timedelta(minutes=40)
    assert renewed.expires_at > lease.expires_at


def test_renew_lease_refuses_a_non_positive_extension() -> None:
    """A renewal that lasts no time grants a lease already dead."""
    with pytest.raises(LeaseTransitionError, match="positive time"):
        renew_lease(lease_in(LeaseStatus.ACTIVE), at=AT, ttl=timedelta(0))


def test_renew_lease_refuses_a_lease_that_is_not_active() -> None:
    """An expired lease is reconciled, never extended back into life."""
    with pytest.raises(LeaseTransitionError, match="cannot renew"):
        renew_lease(lease_in(LeaseStatus.EXPIRED), at=AT, ttl=TTL)


def test_lease_has_expired_is_false_one_tick_before_the_deadline() -> None:
    """The off-by-one boundary below."""
    lease = lease_in(LeaseStatus.ACTIVE)
    assert not lease_has_expired(lease, now=lease.expires_at - timedelta(microseconds=1))


def test_lease_has_expired_is_true_at_the_deadline_itself() -> None:
    """Dead at the instant, so two observers never disagree about it."""
    lease = lease_in(LeaseStatus.ACTIVE)
    assert lease_has_expired(lease, now=lease.expires_at)


# ---- the write boundary ------------------------------------------------------


@pytest.mark.parametrize(
    "relative", ["src/eawf", "src/eawf/kernel", "src/eawf/kernel/runtime/lease.py"]
)
def test_write_admitted_accepts_a_path_under_a_declared_root(relative: str) -> None:
    """The root itself and anything beneath it."""
    assert write_admitted(lease_in(LeaseStatus.ACTIVE), relative=relative)


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "src",
        "src/eawfx",
        "src/eawfx/thing.py",
        "tests/unit/x.py",
        "../outside.py",
        "src/eawf/../../escape.py",
        "src/eawf/./same.py",
        "/etc/passwd",
        "src\\eawf\\thing.py",
        "src/eawf//double.py",
    ],
)
def test_write_admitted_refuses_anything_outside_the_declared_roots(relative: str) -> None:
    """A prefix that is not a path segment, an escape, and an absolute."""
    assert not write_admitted(lease_in(LeaseStatus.ACTIVE), relative=relative)
