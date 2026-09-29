"""The predicates a Task release is judged by, and the facts its edges write."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.task import Task, TaskStatus
from eawf.kernel.state.epoch2.transitions import TransitionGuard
from eawf.runtime.daemon.epoch2_edges import edge_updates, locked_unmet
from eawf.runtime.daemon.methods.domain_guards import (
    GUARD_COMPUTERS,
    GuardInputs,
    task_run_open,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TASK: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
ELSEWHERE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"


def _task(status: str = "CLAIMED", **fields: Any) -> Task:
    return Task.model_validate({**seed_row("task", status), **fields})


def _run(status: str, task_ref: str = TASK) -> dict[str, Any]:
    row = seed_row("run", status)
    row["scope"]["task_ref"] = task_ref
    return row


def _inputs(record: Any, *, actor: str, document: dict[str, Any] | None = None) -> GuardInputs:
    return GuardInputs(
        document=document or {},
        document_path=Path("state.json"),
        record=record,
        updates={},
        reason_code="operator-abort",
        actor=actor,
    )


# ---- DEL-021: only the holder releases, never under an open Run ----------------


@pytest.mark.parametrize(
    ("holder", "actor", "held"),
    [("OP-0001", "OP-0001", True), ("OP-0001", "OP-0002", False), (None, "OP-0002", True)],
)
def test_del_021_the_releaser_must_hold_the_lease(
    holder: str | None, actor: str, held: bool
) -> None:
    computer = GUARD_COMPUTERS[TransitionGuard.RELEASER_HOLDS_LEASE]
    record = _task(**({} if holder is None else {"claimed_by": holder}))

    assert computer(_inputs(record, actor=actor)) is held


def test_del_021_the_lease_guard_answers_only_for_a_task() -> None:
    computer = GUARD_COMPUTERS[TransitionGuard.RELEASER_HOLDS_LEASE]
    run = seed_row("run", "QUEUED")

    assert computer(_inputs(run, actor="OP-0001")) is False


@pytest.mark.parametrize(
    ("status", "open_"),
    [
        ("QUEUED", True),
        ("RUNNING", True),
        ("SUSPENDED", True),
        ("COMPLETED", False),
        ("FAILED", False),
        ("CANCELLED", False),
    ],
)
def test_del_021_only_an_unfinished_run_holds_its_task(status: str, open_: bool) -> None:
    document = {"run": {"RUN-00000010": _run(status)}}

    assert task_run_open(document, _task()) is open_
    computer = GUARD_COMPUTERS[TransitionGuard.NO_ACTIVE_RUN]
    assert computer(_inputs(_task(), actor="OP-0001", document=document)) is not open_


def test_del_021_a_run_for_another_task_does_not_hold_this_one() -> None:
    document = {"run": {"RUN-00000010": _run("RUNNING", task_ref=ELSEWHERE)}}

    assert task_run_open(document, _task()) is False


def test_del_021_a_document_with_no_runs_holds_nothing() -> None:
    assert task_run_open({}, _task()) is False


def test_del_021_an_unreadable_run_row_holds_nothing() -> None:
    assert task_run_open({"run": {"RUN-00000010": {"scope": "garbled"}}}, _task()) is False


def test_del_021_a_run_queued_after_the_preflight_is_judged_again_at_commit() -> None:
    document = {"run": {"RUN-00000010": _run("QUEUED")}}

    unmet = locked_unmet(document, record=_task(), target=TaskStatus.PLANNED)

    assert unmet == frozenset({TransitionGuard.NO_ACTIVE_RUN})
    assert locked_unmet({}, record=_task(), target=TaskStatus.PLANNED) == frozenset()
    assert locked_unmet(document, record=_task(), target=TaskStatus.CANCELLED) == frozenset()


# ---- the facts the claim and release edges write --------------------------------


def test_del_021_a_claim_records_its_holder_and_first_stamp() -> None:
    written = edge_updates(
        _task("PLANNED"), target=TaskStatus.CLAIMED, updates={}, actor="OP-0001", now=AT
    )

    assert written == {"claimed_by": "OP-0001", "first_claimed_at": AT}


def test_del_021_a_second_claim_keeps_the_first_stamp_and_names_its_own_holder() -> None:
    earlier = datetime(2026, 9, 1, tzinfo=UTC)
    record = _task("PLANNED", first_claimed_at=earlier)

    written = edge_updates(record, target=TaskStatus.CLAIMED, updates={}, actor="OP-0002", now=AT)

    assert written == {"claimed_by": "OP-0002"}


def test_del_024_a_release_moves_the_contract_revision_on() -> None:
    written = edge_updates(
        _task(), target=TaskStatus.PLANNED, updates={"x": 1}, actor="OP-0001", now=AT
    )

    assert written == {"x": 1, "contract_revision": 2}


def test_del_024_other_task_edges_write_only_what_they_were_given() -> None:
    written = edge_updates(
        _task(), target=TaskStatus.CANCELLED, updates={}, actor="OP-0001", now=AT
    )

    assert written == {}
