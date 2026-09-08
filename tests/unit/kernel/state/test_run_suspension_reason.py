"""A suspended Run always names the fact that clears it.

``SUSPENDED`` is the one status that waits, so it is the one status the
reason belongs to. These tests pin both directions -- a suspended Run
with no reason and an unsuspended Run carrying one are both refused --
and then pin the partition the Activity surface reads: every reason
lands in exactly one sub-bucket, so a waiting Run is listed once and an
operator scanning "waiting on me" cannot miss it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2 import (
    SUSPENSION_ACTIVITY_BUCKETS,
    ActivityBucket,
    Run,
    RunStatus,
    SuspensionReason,
)

pytestmark = pytest.mark.unit

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"

STARTED = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
ENDED = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)

SCOPE: dict[str, Any] = {
    "scope_kind": "task",
    "purpose": "implement",
    "task_ref": TASK_URN,
    "write_set": ["src/eawf/kernel/state/epoch2/run.py"],
}

FAILURE: dict[str, Any] = {"code": "gate-red", "message": "the pytest gate exited non-zero"}

#: The statuses that are not SUSPENDED; none of them may carry a reason.
UNSUSPENDED = tuple(status for status in RunStatus if status is not RunStatus.SUSPENDED)


def _run(**overrides: Any) -> Run:
    """Build a SUSPENDED Run with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=31),
        "key": "RUN-00000010",
        "urn": RUN_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": STARTED,
        "updated_at": STARTED,
        "scope": SCOPE,
        "status": RunStatus.SUSPENDED,
        "started_at": STARTED,
        "suspension_reason": SuspensionReason.AWAITING_LEASE,
    }
    payload.update(overrides)
    return Run(**payload)


def _stamps_for(status: RunStatus) -> dict[str, Any]:
    """Return the start and end stamps *status* requires."""
    if status is RunStatus.QUEUED:
        return {"started_at": None, "ended_at": None}
    if status in (RunStatus.RUNNING, RunStatus.SUSPENDED):
        return {"started_at": STARTED, "ended_at": None}
    return {"started_at": STARTED, "ended_at": ENDED}


@pytest.mark.parametrize("reason", list(SuspensionReason))
def test_run_accepts_every_suspension_reason(reason: SuspensionReason) -> None:
    """Each of the six reasons is a legal way to be suspended."""
    run = _run(suspension_reason=reason)
    assert run.suspension_reason is reason
    assert run.activity_bucket is SUSPENSION_ACTIVITY_BUCKETS[reason]


def test_run_rejects_a_suspended_run_without_a_reason() -> None:
    """A pause with no clearing fact describes a Run that cannot resume."""
    with pytest.raises(ValidationError, match="requires the suspension_reason"):
        _run(suspension_reason=None)


@pytest.mark.parametrize("status", UNSUSPENDED)
def test_run_rejects_a_suspension_reason_on_a_non_suspended_carrier(status: RunStatus) -> None:
    """Only SUSPENDED carries a reason; no other status waits for anything."""
    overrides: dict[str, Any] = {"status": status, **_stamps_for(status)}
    if status is RunStatus.FAILED:
        overrides["failure"] = FAILURE
    with pytest.raises(ValidationError, match="belongs to a SUSPENDED Run"):
        _run(**overrides)


def test_run_activity_bucket_is_none_when_not_suspended() -> None:
    """A running Run waits for nothing, so it is in no waiting bucket."""
    run = _run(status=RunStatus.RUNNING, suspension_reason=None)
    assert run.activity_bucket is None


def test_suspension_activity_buckets_partition_every_reason() -> None:
    """Every reason lands in exactly one Activity sub-bucket key."""
    assert set(SUSPENSION_ACTIVITY_BUCKETS) == set(SuspensionReason)
    preimages: dict[ActivityBucket, set[SuspensionReason]] = defaultdict(set)
    for reason, bucket in SUSPENSION_ACTIVITY_BUCKETS.items():
        assert isinstance(bucket, ActivityBucket)
        preimages[bucket].add(reason)
    members = [reason for bucket in preimages.values() for reason in bucket]
    assert len(members) == len(SuspensionReason)
    assert set(members) == set(SuspensionReason)
    assert set(preimages) <= set(ActivityBucket)


def test_run_rejects_an_unknown_suspension_reason() -> None:
    """The reason vocabulary is closed."""
    with pytest.raises(ValidationError, match="suspension_reason"):
        _run(suspension_reason="AWAITING_COFFEE")


def test_run_rejects_a_failure_reason_outside_the_failed_status() -> None:
    """A failure reason on a suspended Run claims an outcome it has not reached."""
    with pytest.raises(ValidationError, match="belongs to a FAILED Run"):
        _run(failure=FAILURE)


def test_run_requires_a_failure_reason_when_it_failed() -> None:
    """A FAILED Run without a reason records no cause at all."""
    with pytest.raises(ValidationError, match="requires a failure reason"):
        _run(status=RunStatus.FAILED, suspension_reason=None, ended_at=ENDED)


def test_run_rejects_a_started_stamp_on_a_queued_run() -> None:
    """A queued Run has not started, so it carries no start stamp."""
    with pytest.raises(ValidationError, match="disagrees with started_at"):
        _run(status=RunStatus.QUEUED, suspension_reason=None, started_at=STARTED)


def test_run_requires_an_end_stamp_when_it_stopped() -> None:
    """A stopped Run's duration is a recorded fact, not a guess against now."""
    with pytest.raises(ValidationError, match="disagrees with ended_at"):
        _run(status=RunStatus.COMPLETED, suspension_reason=None, ended_at=None)


def test_run_rejects_an_end_that_precedes_its_start() -> None:
    """A Run cannot finish before it began."""
    with pytest.raises(ValidationError, match="ended_at precedes started_at"):
        _run(
            status=RunStatus.COMPLETED,
            suspension_reason=None,
            started_at=ENDED,
            ended_at=STARTED,
        )


def test_run_rejects_a_urn_addressing_another_run() -> None:
    """The key and the locator name one record or the record is refused."""
    with pytest.raises(ValidationError, match="carries a URN addressing"):
        _run(key="RUN-00000011")


def test_run_rejects_a_key_of_the_wrong_width() -> None:
    """The eight-digit run key is grammar, not a rendering choice."""
    with pytest.raises(ValidationError, match="RUN"):
        _run(key="RUN-0010", urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-0010")


def test_run_rejects_an_unknown_field() -> None:
    """An unknown key is a defect, not a value to absorb."""
    with pytest.raises(ValidationError, match="agent_role"):
        _run(agent_role="executor")
