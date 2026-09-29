"""AUTH-019 and AUTH-021: the WIP ceilings and the backlog's draft head, over a real canary.

AUTH-019 splits the two work-in-progress limits by whether a mechanism
enforces them. ``wip.active_batches_per_repo`` refuses a Batch activation
past the ceiling -- counted per Track per repository, and judged again
under the document lock -- while ``wip.active_milestones`` never refuses
and is surfaced as a watchlist line on the accepted answer.

AUTH-021 makes the backlog the draft head of the Task lifecycle. Demotion
back to ``DRAFT`` is legal only before the first claim, which the first
claim stamps and a released lease does not clear; a demoted Task keeps its
identity and leaves its Batch's list. ``DEFERRED`` may be dropped
directly. Promotion through plan apply is proved in
``tests/integration/workflow/planning/test_plan_apply_promotes_backlog.py``.
"""

from __future__ import annotations

import asyncio
import copy
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.methods.domain_guards import WIP_WATCHLIST_SIGNAL
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
BRANCH = "feature/eawf-v0.7"
SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
OTHER_REPOSITORY = f"{SLOT}/repository/REP-OTHER"
OTHER_TRACK = f"{SLOT}/track/TRK-OTHER"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _canary(tmp_path: Path, rows: dict[str, dict[str, Any]]) -> CanaryProvision:
    canary = provision(tmp_path / "repo", code="WIP")
    seed(canary, rows)
    return canary


def _call(
    canary: CanaryProvision, tmp_path: Path, method: str, urn: str, revision: int, **params: Any
) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    payload: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": urn,
        "expected_revision": revision,
        "idempotency_key": f"{method}-{urn.rsplit('/', 1)[1]}-{revision}",
        "actor": ACTOR,
        **params,
    }
    return asyncio.run(methods.dispatch(method, ctx, payload))


def _rows(canary: CanaryProvision, collection: str) -> dict[str, Any]:
    rows: dict[str, Any] = read_document(document_path(canary)).get(collection, {})
    return rows


def _track(*, batches: int = 1, milestones: int = 1, key: str = "TRK-RUNTIME") -> dict[str, Any]:
    row = rekeyed(seed_row("track", "ACTIVE"), key=key)
    row["policy"]["wip"] = {"active_milestones": milestones, "active_batches_per_repo": batches}
    return row


def _milestone(status: str, *, key: str = "MLS-0030", track: str | None = None) -> dict[str, Any]:
    row = rekeyed(seed_row("milestone", status), key=key)
    if track is not None:
        row["primary_track_ref"] = track
    return row


def _batch(status: str, *, key: str = "BAT-0007", **overrides: Any) -> dict[str, Any]:
    row = rekeyed(seed_row("batch", status), key=key)
    row.update(overrides)
    return row


def _activation_world(**second: Any) -> dict[str, dict[str, Any]]:
    """A PLANNED Batch beside a second, ACTIVE one shaped by *second*."""
    track_batches = second.pop("track_batches", 1)
    extra_rows = second.pop("extra", {})
    rows: dict[str, dict[str, Any]] = {
        "track": {"TRK-RUNTIME": _track(batches=track_batches)},
        "milestone": {"MLS-0030": _milestone("ACTIVE")},
        "batch": {
            "BAT-0007": _batch("PLANNED"),
            "BAT-0008": _batch("ACTIVE", key="BAT-0008", **second),
        },
    }
    for collection, members in extra_rows.items():
        rows.setdefault(collection, {}).update(members)
    return rows


def _activate(canary: CanaryProvision, tmp_path: Path) -> dict[str, Any]:
    return _call(
        canary,
        tmp_path,
        "domain.batch.activate",
        BATCH_URN,
        1,
        updates={"target_branch": BRANCH},
    )


# ---- AUTH-019: the hard per-repository Batch ceiling -------------------------


def test_auth_019_a_second_active_batch_in_one_repository_is_refused(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _activation_world())
    before = document_path(canary).read_bytes()

    answer = _activate(canary, tmp_path)

    assert answer["status"] == "error"
    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert row["guard"] == "batch_wip_admits"
    assert "active Batch" in row["remediation"]
    assert document_path(canary).read_bytes() == before


def test_auth_019_the_ceiling_admits_up_to_its_value(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _activation_world(track_batches=2))

    answer = _activate(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert _rows(canary, "batch")["BAT-0007"]["status"] == "ACTIVE"


def test_auth_019_a_batch_active_in_another_repository_is_not_counted(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _activation_world(repository_ref=OTHER_REPOSITORY))

    assert _activate(canary, tmp_path)["status"] == "ok"


def test_auth_019_another_tracks_active_batch_is_not_counted(tmp_path: Path) -> None:
    other_milestone = f"{SLOT}/milestone/MLS-0031"
    canary = _canary(
        tmp_path,
        _activation_world(
            milestone_ref=other_milestone,
            extra={
                "track": {"TRK-OTHER": _track(key="TRK-OTHER")},
                "milestone": {"MLS-0031": _milestone("ACTIVE", key="MLS-0031", track=OTHER_TRACK)},
            },
        ),
    )

    assert _activate(canary, tmp_path)["status"] == "ok"


def test_auth_019_a_planned_sibling_batch_is_not_counted(tmp_path: Path) -> None:
    rows = _activation_world()
    rows["batch"]["BAT-0008"] = _batch("PLANNED", key="BAT-0008")
    canary = _canary(tmp_path, rows)

    assert _activate(canary, tmp_path)["status"] == "ok"


def test_auth_019_a_batch_whose_track_cannot_be_read_is_refused(tmp_path: Path) -> None:
    canary = _canary(tmp_path, {"batch": {"BAT-0007": _batch("PLANNED")}})

    answer = _activate(canary, tmp_path)

    assert answer["errors"][0]["guard"] == "batch_wip_admits"


def test_auth_019_the_ceiling_is_judged_again_under_the_document_lock(tmp_path: Path) -> None:
    """A caller that skips the preflight still meets the ceiling at commit."""
    canary = _canary(tmp_path, _activation_world())
    request = TransitionRequest(
        urn=BATCH_URN,
        to_status="ACTIVE",
        expected_revision=1,
        idempotency_key="direct-activate",
        actor=ACTOR,
        updates={"target_branch": BRANCH},
    )

    with pytest.raises(TransactionRefusedError) as refused:
        run_transaction(context=root_context(canary, tmp_path / "rt"), request=request, now=AT)

    assert refused.value.guard == "batch_wip_admits"


# ---- AUTH-019: the advisory Milestone ceiling --------------------------------


def _milestone_world(*, advisory: int) -> dict[str, dict[str, Any]]:
    return {
        "track": {"TRK-RUNTIME": _track(milestones=advisory)},
        "milestone": {
            "MLS-0030": _milestone("PLANNED"),
            "MLS-0031": _milestone("ACTIVE", key="MLS-0031"),
        },
    }


def test_auth_019_the_milestone_ceiling_never_refuses_and_is_surfaced(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _milestone_world(advisory=1))

    answer = _call(canary, tmp_path, "domain.milestone.activate", MILESTONE_URN, 1)

    assert answer["status"] == "ok", answer["errors"]
    assert _rows(canary, "milestone")["MLS-0030"]["status"] == "ACTIVE"
    (line,) = [note for note in answer["warnings"] if note.startswith(WIP_WATCHLIST_SIGNAL)]
    assert "2 active milestones, past its advisory 1" in line


def test_auth_019_a_milestone_within_its_advisory_raises_no_signal(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _milestone_world(advisory=2))

    answer = _call(canary, tmp_path, "domain.milestone.activate", MILESTONE_URN, 1)

    assert answer["status"] == "ok"
    assert not any(note.startswith(WIP_WATCHLIST_SIGNAL) for note in answer["warnings"])


# ---- AUTH-021: demotion before the first claim only --------------------------


def _planned_in_batch() -> dict[str, dict[str, Any]]:
    return {
        "task": {"EAWF-0042": seed_row("task", "PLANNED")},
        "batch": {"BAT-0007": _batch("ACTIVE", task_refs=[TASK_URN])},
    }


def test_auth_021_a_never_claimed_task_demotes_to_a_draft_keeping_its_identity(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, _planned_in_batch())
    before = copy.deepcopy(_rows(canary, "task")["EAWF-0042"])

    answer = _call(
        canary, tmp_path, "domain.task.demote", TASK_URN, 1, reason_code="back-to-backlog"
    )

    assert answer["status"] == "ok", answer["errors"]
    assert answer["result"]["event_name"] == "domain.task.demoted"
    task = _rows(canary, "task")["EAWF-0042"]
    assert task["status"] == "DRAFT"
    assert (task["uid"], task["key"], task["urn"]) == (before["uid"], before["key"], before["urn"])
    assert task["batch_ref"] is None
    assert task["criteria"] == []
    batch = _rows(canary, "batch")["BAT-0007"]
    assert batch["task_refs"] == []
    assert batch["revision"] == 2


def test_auth_021_demotion_needs_a_reason(tmp_path: Path) -> None:
    canary = _canary(tmp_path, _planned_in_batch())

    answer = _call(canary, tmp_path, "domain.task.demote", TASK_URN, 1)

    assert answer["errors"][0]["guard"] == "reason_recorded"


def test_auth_021_a_claim_closes_demotion_even_after_the_lease_is_released(
    tmp_path: Path,
) -> None:
    canary = _canary(tmp_path, _planned_in_batch())
    assert _call(canary, tmp_path, "domain.task.claim", TASK_URN, 1)["status"] == "ok"
    claimed = _rows(canary, "task")["EAWF-0042"]
    assert claimed["first_claimed_at"] is not None
    released = run_transaction(
        context=root_context(canary, tmp_path / "rt"),
        request=TransitionRequest(
            urn=TASK_URN,
            to_status="PLANNED",
            expected_revision=2,
            idempotency_key="release-lease",
            actor=ACTOR,
            reason_code="lease-expired",
        ),
        now=AT,
    )
    assert released.receipt.event_name == "domain.task.lease_released"
    assert _rows(canary, "task")["EAWF-0042"]["first_claimed_at"] == claimed["first_claimed_at"]

    answer = _call(
        canary, tmp_path, "domain.task.demote", TASK_URN, 3, reason_code="back-to-backlog"
    )

    assert answer["errors"][0]["guard"] == "never_claimed"
    assert _rows(canary, "task")["EAWF-0042"]["status"] == "PLANNED"


def test_auth_021_a_second_claim_keeps_the_first_claim_stamp(tmp_path: Path) -> None:
    first = "2026-09-01T00:00:00Z"
    row = {**seed_row("task", "PLANNED"), "first_claimed_at": first}
    canary = _canary(tmp_path, {"task": {"EAWF-0042": row}})

    assert _call(canary, tmp_path, "domain.task.claim", TASK_URN, 1)["status"] == "ok"

    assert _rows(canary, "task")["EAWF-0042"]["first_claimed_at"] == first


def test_auth_021_a_deferred_draft_is_dropped_directly(tmp_path: Path) -> None:
    canary = _canary(tmp_path, {"task": {"EAWF-0042": seed_row("task", "DEFERRED")}})

    committed = run_transaction(
        context=root_context(canary, tmp_path / "rt"),
        request=TransitionRequest(
            urn=TASK_URN,
            to_status="DROPPED",
            expected_revision=1,
            idempotency_key="drop-deferred",
            actor=ACTOR,
            reason_code="no-longer-wanted",
        ),
        now=AT,
    )

    assert committed.receipt.event_name == "domain.task.dropped"


def test_auth_021_a_backlog_row_carrying_a_claim_stamp_does_not_validate() -> None:
    row = {**seed_row("task", "DRAFT"), "first_claimed_at": "2026-09-01T00:00:00Z"}
    with pytest.raises(ValidationError, match="was never claimed"):
        Task.model_validate(row)
