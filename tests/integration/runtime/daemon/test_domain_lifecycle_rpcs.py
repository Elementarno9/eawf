"""Every per-entity lifecycle verb commits once, or refuses and writes nothing.

Each registered verb is driven twice against a freshly provisioned canary:
once over a document that satisfies the edge, where the answer is an ``ok``
envelope whose receipt names one event from the closed
``domain.<entity>.<verb>`` vocabulary, and once over a document that does
not, where the answer is an ``error`` envelope carrying the stable code of
whatever refused it and the tree is byte-for-byte what it was.

The denied cases are deliberately about predicates the document can
answer. A registry guard that only an observation can satisfy is already
shut by the reducer; the interesting failure is the one the reducer
defaults to satisfied -- a Milestone activating under a retired Track, a
Batch declaring itself mergeable with a Task still running -- because that
is the edge a per-entity verb has to close itself.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.domain_events import DOMAIN_EVENT_NAMES
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_METHODS
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.wal import list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

ACTOR = "OP-0001"
KEY = "req-lifecycle-0001"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
APPROVAL_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
BRANCH = "feature/eawf-v0.7"

_BINDING = seed_row("milestone", "COMPLETED")["accepted_binding"]
_HEAD_BINDING = seed_row("batch", "READY_TO_MERGE")["current_head_binding"]
_CRITERIA = seed_row("task", "PLANNED")["criteria"]


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _milestone(status: str, **overrides: Any) -> dict[str, Any]:
    """Return the seeded Milestone at *status* with *overrides* applied."""
    row = seed_row("milestone", status)
    row.update(overrides)
    return row


def _batch(status: str, **overrides: Any) -> dict[str, Any]:
    """Return the seeded Batch at *status* with *overrides* applied."""
    row = seed_row("batch", status)
    row.update(overrides)
    return row


@dataclass(frozen=True)
class Case:
    """One drive of one verb, and what the answer must be.

    Attributes:
        method: The verb under test.
        rows: The document rows the canary is seeded with.
        urn: The record the request addresses.
        params: The request parameters beyond the four every verb takes.
        event_name: The event a committed case must carry.
        code: The stable code a refused case must carry.
        guard: The guard name a refused case must name, when a predicate
            was reached.
    """

    method: str
    rows: dict[str, dict[str, Any]]
    urn: str
    params: dict[str, Any] = field(default_factory=dict)
    event_name: str = ""
    code: str = ""
    guard: str | None = None


COMMITTED_CASES: tuple[Case, ...] = (
    Case(
        method="domain.track.retire",
        rows={"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}},
        urn=TRACK_URN,
        event_name="domain.track.retired",
    ),
    Case(
        method="domain.milestone.activate",
        rows={
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "PLANNED")},
        },
        urn=MILESTONE_URN,
        event_name="domain.milestone.activated",
    ),
    Case(
        method="domain.milestone.open_review",
        rows={"milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")}},
        urn=MILESTONE_URN,
        params={"updates": {"acceptance_bundle_revision": 1}},
        event_name="domain.milestone.review_opened",
    ),
    Case(
        method="domain.milestone.accept",
        rows={"milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")}},
        urn=MILESTONE_URN,
        params={
            "updates": {"accepted_binding": _BINDING},
            "approval_receipt_ref": APPROVAL_URN,
        },
        event_name="domain.milestone.completed",
    ),
    Case(
        method="domain.milestone.cancel",
        rows={"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}},
        urn=MILESTONE_URN,
        params={"reason_code": "operator-cancelled"},
        event_name="domain.milestone.cancelled",
    ),
    Case(
        method="domain.batch.activate",
        rows={"batch": {"BAT-0007": seed_row("batch", "PLANNED")}},
        urn=BATCH_URN,
        params={"updates": {"target_branch": BRANCH}},
        event_name="domain.batch.activated",
    ),
    Case(
        method="domain.batch.ready",
        rows={"batch": {"BAT-0007": seed_row("batch", "ACTIVE")}},
        urn=BATCH_URN,
        params={"updates": {"current_head_binding": _HEAD_BINDING}},
        event_name="domain.batch.ready",
    ),
    Case(
        method="domain.task.promote",
        rows={"task": {"EAWF-0042": seed_row("task", "DRAFT")}},
        urn=TASK_URN,
        params={
            "updates": {
                "batch_ref": BATCH_URN,
                "criteria": _CRITERIA,
                "due_scope": MILESTONE_URN,
            }
        },
        event_name="domain.task.promoted",
    ),
    Case(
        method="domain.task.start",
        rows={
            "task": {"EAWF-0042": seed_row("task", "CLAIMED")},
            "run": {"RUN-00000010": seed_row("run", "QUEUED")},
        },
        urn=TASK_URN,
        params={"updates": {"active_run_ref": RUN_URN}},
        event_name="domain.task.started",
    ),
)


REFUSED_CASES: tuple[Case, ...] = (
    Case(
        method="domain.track.retire",
        rows={
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "milestone": {"MLS-0030": seed_row("milestone", "PLANNED")},
        },
        urn=TRACK_URN,
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="no_open_milestones",
    ),
    Case(
        method="domain.milestone.activate",
        rows={
            "track": {"TRK-RUNTIME": seed_row("track", "RETIRED")},
            "milestone": {"MLS-0030": seed_row("milestone", "PLANNED")},
        },
        urn=MILESTONE_URN,
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="track_active",
    ),
    Case(
        method="domain.milestone.open_review",
        rows={
            "milestone": {
                "MLS-0030": _milestone("ACTIVE", required_batch_refs=[BATCH_URN]),
            },
            "batch": {"BAT-0007": seed_row("batch", "ACTIVE")},
        },
        urn=MILESTONE_URN,
        params={"updates": {"acceptance_bundle_revision": 1}},
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="required_batches_completed",
    ),
    Case(
        method="domain.milestone.accept",
        rows={"milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")}},
        urn=MILESTONE_URN,
        params={"updates": {"accepted_binding": _BINDING}},
        code=DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value,
        guard="acceptance_journey_passed",
    ),
    Case(
        method="domain.milestone.cancel",
        rows={"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}},
        urn=MILESTONE_URN,
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="reason_recorded",
    ),
    Case(
        method="domain.batch.activate",
        rows={"batch": {"BAT-0007": seed_row("batch", "PLANNED")}},
        urn=BATCH_URN,
        params={"updates": {"target_branch": None}},
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="target_branch_pinned",
    ),
    Case(
        method="domain.batch.ready",
        rows={
            "batch": {"BAT-0007": _batch("ACTIVE", task_refs=[TASK_URN])},
            "task": {"EAWF-0042": seed_row("task", "RUNNING")},
        },
        urn=BATCH_URN,
        params={"updates": {"current_head_binding": _HEAD_BINDING}},
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="tasks_ready_to_integrate",
    ),
    Case(
        method="domain.task.promote",
        rows={"task": {"EAWF-0042": seed_row("task", "DRAFT")}},
        urn=TASK_URN,
        params={
            "updates": {
                "batch_ref": BATCH_URN,
                "criteria": [],
                "due_scope": MILESTONE_URN,
            }
        },
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="promotion_contract_complete",
    ),
    Case(
        method="domain.task.start",
        rows={"task": {"EAWF-0042": seed_row("task", "CLAIMED")}},
        urn=TASK_URN,
        params={"updates": {"active_run_ref": RUN_URN}},
        code=DomainErrorCode.TRANSITION_GUARD_FAILED.value,
        guard="run_bound",
    ),
)


def _seeded(case: Case, tmp_path: Path, *, code: str) -> CanaryProvision:
    """Provision a canary holding this case's document rows."""
    canary = provision(tmp_path / code.lower(), code=code)
    seed(canary, case.rows)
    return canary


def _drive(
    case: Case,
    canary: CanaryProvision,
    tmp_path: Path,
    *,
    bus: EventBus | None = None,
    key: str = KEY,
) -> dict[str, Any]:
    """Dispatch this case's verb against *canary* and return the envelope."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = bus
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": case.urn,
        "expected_revision": 1,
        "idempotency_key": key,
        "actor": ACTOR,
        **case.params,
    }
    return asyncio.run(methods.dispatch(case.method, ctx, params))


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return every row the canary's firehose holds."""
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _wal_records(canary: CanaryProvision, tmp_path: Path) -> list[Path]:
    """Return every WAL record filed under the canary's native namespace."""
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    return list(list_records(context.wal_dir))


@pytest.mark.parametrize("case", COMMITTED_CASES, ids=lambda case: case.method)
def test_lifecycle_verb_commits_through_the_transaction(case: Case, tmp_path: Path) -> None:
    canary = _seeded(case, tmp_path, code="OK")

    answer = _drive(case, canary, tmp_path)

    assert answer["errors"] == []
    assert answer["status"] == "ok"
    assert answer["operation"] == case.method
    assert answer["result"]["event_name"] == case.event_name
    assert case.event_name in DOMAIN_EVENT_NAMES
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    rows = _firehose_rows(canary)
    assert [row["payload"]["name"] for row in rows] == [case.event_name]
    assert rows[0]["payload"]["canonical_sequence"] == answer["result"]["canonical_sequence"]
    assert len(_wal_records(canary, tmp_path)) == 1


@pytest.mark.parametrize("case", REFUSED_CASES, ids=lambda case: case.method)
def test_lifecycle_verb_refuses_with_its_stable_code_and_writes_nothing(
    case: Case, tmp_path: Path
) -> None:
    canary = _seeded(case, tmp_path, code="DENY")
    before = document_path(canary).read_bytes()

    answer = _drive(case, canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["result"] is None
    assert answer["operation"] == case.method
    row = answer["errors"][0]
    assert row["code"] == case.code
    assert row["guard"] == case.guard
    assert row["entity_ref"] == case.urn
    assert row["remediation"]
    assert answer["revision_before"] == answer["revision_after"] == 1
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []
    assert _wal_records(canary, tmp_path) == []


def test_committed_verb_publishes_one_envelope_after_the_commit(tmp_path: Path) -> None:
    case = COMMITTED_CASES[1]
    canary = _seeded(case, tmp_path, code="PUB")
    bus = EventBus()
    published: list[Envelope] = []
    bus.publish = published.append  # type: ignore[method-assign]

    _drive(case, canary, tmp_path, bus=bus)

    assert [envelope.payload["name"] for envelope in published] == [case.event_name]


def test_retry_replays_the_receipt_and_publishes_nothing(tmp_path: Path) -> None:
    """A replayed answer carries no envelope, so the retry fans out nothing."""
    case = COMMITTED_CASES[1]
    canary = _seeded(case, tmp_path, code="RETRY")
    bus = EventBus()
    published: list[Envelope] = []
    bus.publish = published.append  # type: ignore[method-assign]

    first = _drive(case, canary, tmp_path, bus=bus)
    second = _drive(case, canary, tmp_path, bus=bus)

    assert second["status"] == "ok"
    assert second["result"] == first["result"]
    assert len(published) == 1
    assert len(_firehose_rows(canary)) == 1


def test_retry_with_other_parameters_is_an_idempotency_conflict(tmp_path: Path) -> None:
    case = COMMITTED_CASES[4]
    canary = _seeded(case, tmp_path, code="CONFLICT")

    _drive(case, canary, tmp_path)
    other = Case(
        method=case.method,
        rows=case.rows,
        urn=case.urn,
        params={"reason_code": "scope-withdrawn"},
    )
    answer = _drive(other, canary, tmp_path)

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(_firehose_rows(canary)) == 1


def test_verb_refuses_a_record_in_a_status_it_does_not_move(tmp_path: Path) -> None:
    """A Task leaving CLAIMED for PLANNED is another verb's edge, not promote's."""
    case = Case(
        method="domain.task.promote",
        rows={"task": {"EAWF-0042": seed_row("task", "CLAIMED")}},
        urn=TASK_URN,
        params={"reason_code": "lease-expired"},
    )
    canary = _seeded(case, tmp_path, code="WRONGSRC")
    before = document_path(canary).read_bytes()

    answer = _drive(case, canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert answer["errors"][0]["guard"] is None
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []


def test_verb_refuses_a_urn_addressing_another_entity(tmp_path: Path) -> None:
    """Cancelling a Milestone must never cancel a Task that shares the status."""
    case = Case(
        method="domain.milestone.cancel",
        rows={"task": {"EAWF-0042": seed_row("task", "PLANNED")}},
        urn=TASK_URN,
        params={"reason_code": "operator-cancelled"},
    )
    canary = _seeded(case, tmp_path, code="WRONGKIND")
    before = document_path(canary).read_bytes()

    answer = _drive(case, canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_KIND_MISMATCH.value
    assert answer["revision_before"] is None
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []
    assert _wal_records(canary, tmp_path) == []


def test_verb_leaves_an_absent_record_to_the_transaction(tmp_path: Path) -> None:
    """The empty boundary: a document with no rows refuses identity_not_found."""
    case = Case(method="domain.milestone.activate", rows={}, urn=MILESTONE_URN)
    canary = _seeded(case, tmp_path, code="BARE")

    answer = _drive(case, canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_NOT_FOUND.value
    assert answer["revision_before"] is None


def test_verb_leaves_a_stale_revision_to_the_transaction(tmp_path: Path) -> None:
    """Off by one against the stored revision is a conflict, not a guard failure."""
    case = COMMITTED_CASES[1]
    canary = _seeded(case, tmp_path, code="STALE")
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch(
            case.method,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": case.urn,
                "expected_revision": 2,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert answer["revision_before"] == 1


def test_verb_refuses_an_unparsable_request_without_leaking_values(tmp_path: Path) -> None:
    case = COMMITTED_CASES[1]
    canary = _seeded(case, tmp_path, code="BADREQ")
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch(case.method, ctx, {"repo_root": str(canary.root), "urn": case.urn})
    )

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "expected_revision" in row["message"]
    assert "idempotency_key" in row["message"]
    assert str(tmp_path) not in row["message"]


def test_every_lifecycle_verb_is_registered_on_the_server() -> None:
    """Importing the server registers all nine per-entity verbs."""
    import eawf.runtime.daemon.server  # noqa: F401

    registered = methods.registered_methods()
    assert [name for name in DOMAIN_LIFECYCLE_METHODS if name not in registered] == []


def test_each_entity_family_has_a_registered_verb() -> None:
    families = {name.split(".")[1] for name in DOMAIN_LIFECYCLE_METHODS}
    assert families == {"track", "milestone", "batch", "task"}


def test_committed_and_refused_cases_cover_every_verb() -> None:
    assert [case.method for case in COMMITTED_CASES] == list(DOMAIN_LIFECYCLE_METHODS)
    assert [case.method for case in REFUSED_CASES] == list(DOMAIN_LIFECYCLE_METHODS)
