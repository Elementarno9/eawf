"""A child plan revision may append to an ACTIVE Milestone, and do nothing else.

Each walk applies a parent plan over a canary, marks its Milestone ACTIVE
with one Batch and one Task already underway, then drives a child
revision through submit, approve and apply. An append -- new Batches and
Tasks, required Batch refs added after the ones the Milestone already
has, or a Batch of its own the Milestone does not yet require -- lands
and leaves every existing row untouched. Any edit or removal is refused
under the same guard as before and writes nothing, and a PLANNED
Milestone still re-plans the way it always did. A terminal Task need only
be restated under its own Batch, since the append never writes it, and a
Task or Batch compacted into its ledger counts as placed all the same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import compact_terminal_record
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods.planning import PLAN_SUBMIT_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    seed,
)
from tests.integration.workflow.planning.test_plan_revision_apply import (
    ACTOR,
    BATCH_A,
    BATCH_B,
    BATCH_C,
    CHILD_KEY,
    IMPORTED_PROJECT_ROW,
    OPERATOR,
    OTHER_MILESTONE_URN,
    PARENT_BODY,
    PARENT_KEY,
    TASK_1,
    TASK_2,
    TASK_3,
    TASK_4,
    apply_child,
    apply_plan_key,
    collection,
    dispatch,
    drive,
    edit_row,
    plan_body,
    planned_canary,
)

pytestmark = pytest.mark.integration

GUARD: Final = "replanned_milestone_planned"
OTHER_KEY: Final = "PRV-0020"

#: What the parent placed, restated unchanged, as every append child must.
KEPT: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    BATCH_A: ((TASK_1, "CR-01"), (TASK_2, "CR-01")),
    BATCH_B: ((TASK_3, "CR-01"),),
}

#: The fields a stored Task needs to read as COMPLETED.
COMPLETED: Final[dict[str, Any]] = {
    "status": "COMPLETED",
    "integrated_binding": {
        "head_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "contract_digest": "sha256:" + "c" * 64,
        "policy_revision": 1,
        "evidence_digest": "sha256:" + "d" * 64,
    },
}

#: The fields a stored Task needs to read as DROPPED: unplaced, with no criteria.
DROPPED: Final[dict[str, Any]] = {"status": "DROPPED", "batch_ref": None, "criteria": []}

COMPACTED_AT: Final = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def compact(canary: CanaryProvision, collection_name: str, key: str) -> None:
    """Move one stored row out of the document into its ledger, as compaction does."""
    row = collection(canary, collection_name)[key]
    compact_terminal_record(
        document_path(canary),
        record=LedgerRecord(
            collection=Epoch2Collection(collection_name),
            record_key=key,
            status=row["status"],
            recorded_at=COMPACTED_AT,
            payload=row,
        ),
    )


def ledger_bytes(canary: CanaryProvision) -> dict[str, bytes | None]:
    """Return the Batch and Task ledgers' bytes by collection, ``None`` for one not yet written."""
    paths = {
        name: ledger_path(document_path(canary), Epoch2Collection(name))
        for name in ("batch", "task")
    }
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}


def child(
    batches: dict[str, tuple[tuple[str, str], ...]],
    *,
    required: tuple[str, ...] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return a child body placing *batches* and requiring *required*.

    Args:
        batches: Each Batch URN mapped to its ``(task_urn, criterion_id)``
            entries, in order.
        required: The required Batch refs in order, or ``None`` to
            require every Batch in *batches*.
        **overrides: Passed through to :func:`plan_body`.

    Returns:
        A body the submit verb accepts.
    """
    body = plan_body(batches=batches, **overrides)
    if required is not None:
        body["milestone"]["required_batch_refs"] = list(required)
    return body


def active_parent(
    tmp_path: Path, *, code: str, body: dict[str, Any] = PARENT_BODY
) -> CanaryProvision:
    """Return a canary whose parent *body* is APPLIED and whose Milestone is underway.

    The Milestone is ACTIVE, Batch BAT-0001 is ACTIVE and Task EAWF-0001
    is CLAIMED, so an append is proved to leave started work alone.
    """
    canary = planned_canary(tmp_path, code=code)
    seed(canary, {"project": {"EAWF": IMPORTED_PROJECT_ROW}})
    answer = drive(canary, tmp_path, key=PARENT_KEY, body=body)
    assert answer["status"] == "ok", answer["errors"]
    edit_row(canary, "milestone", "MLS-0030", status="ACTIVE")
    edit_row(canary, "batch", "BAT-0001", status="ACTIVE")
    edit_row(canary, "task", "EAWF-0001", status="CLAIMED")
    return canary


def apply_append(canary: CanaryProvision, tmp_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Drive *body* as the child of the applied parent and return the apply envelope."""
    return drive(canary, tmp_path, key=CHILD_KEY, body=body, parent_key=PARENT_KEY)


def test_appending_a_new_batch_and_task_grows_the_required_batches(tmp_path: Path) -> None:
    """New rows are created and every row the Milestone already had is left as it was."""
    canary = active_parent(tmp_path, code="APNEW")
    batches_before = collection(canary, "batch")
    tasks_before = collection(canary, "task")
    milestone_before = collection(canary, "milestone")["MLS-0030"]

    answer = apply_append(canary, tmp_path, child({**KEPT, BATCH_C: ((TASK_4, "CR-01"),)}))

    assert answer["status"] == "ok", answer["errors"]
    milestone = collection(canary, "milestone")["MLS-0030"]
    assert milestone["status"] == "ACTIVE"
    assert milestone["revision"] == milestone_before["revision"] + 1
    assert milestone["required_batch_refs"] == [BATCH_A, BATCH_B, BATCH_C]
    assert milestone["title"] == milestone_before["title"]
    batches = collection(canary, "batch")
    assert {key: batches[key] for key in batches_before} == batches_before
    assert (batches["BAT-0003"]["status"], batches["BAT-0003"]["revision"]) == ("PLANNED", 1)
    assert batches["BAT-0003"]["milestone_ref"] == MILESTONE_URN
    assert batches["BAT-0003"]["task_refs"] == [TASK_4]
    tasks = collection(canary, "task")
    assert {key: tasks[key] for key in tasks_before} == tasks_before
    assert (tasks["EAWF-0004"]["status"], tasks["EAWF-0004"]["batch_ref"]) == ("PLANNED", BATCH_C)
    assert collection(canary, "plan_revision")[CHILD_KEY]["status"] == "APPLIED"


def test_appending_an_existing_unrequired_batch_of_the_milestone_is_admitted(
    tmp_path: Path,
) -> None:
    """A Batch the Milestone already owns but does not require joins the required list."""
    parent = child(KEPT, required=(BATCH_A,))
    canary = active_parent(tmp_path, code="APOWN", body=parent)
    assert collection(canary, "milestone")["MLS-0030"]["required_batch_refs"] == [BATCH_A]
    batches_before = collection(canary, "batch")
    tasks_before = collection(canary, "task")

    answer = apply_append(canary, tmp_path, child(KEPT))

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "milestone")["MLS-0030"]["required_batch_refs"] == [
        BATCH_A,
        BATCH_B,
    ]
    assert collection(canary, "batch") == batches_before
    assert collection(canary, "task") == tasks_before


def test_an_empty_append_only_restamps_the_milestone(tmp_path: Path) -> None:
    """A child restating the current scope exactly changes nothing but the revision."""
    canary = active_parent(tmp_path, code="APNONE")
    milestone_before = collection(canary, "milestone")["MLS-0030"]
    batches_before = collection(canary, "batch")
    tasks_before = collection(canary, "task")

    answer = apply_append(canary, tmp_path, child(KEPT))

    assert answer["status"] == "ok", answer["errors"]
    milestone = collection(canary, "milestone")["MLS-0030"]
    assert milestone["required_batch_refs"] == milestone_before["required_batch_refs"]
    assert milestone["revision"] == milestone_before["revision"] + 1
    assert collection(canary, "batch") == batches_before
    assert collection(canary, "task") == tasks_before


@pytest.mark.parametrize(
    ("body", "clause"),
    [
        pytest.param(
            child(KEPT, required=(BATCH_A,)), "removes or reorders", id="required-batch-removed"
        ),
        pytest.param(
            child(KEPT, required=(BATCH_B, BATCH_A)),
            "removes or reorders",
            id="required-batches-reordered",
        ),
        pytest.param(
            child({**KEPT, BATCH_C: ((TASK_4, "CR-01"),)}, required=(BATCH_A, BATCH_C)),
            "removes or reorders",
            id="append-in-place-of-a-required-batch",
        ),
        pytest.param(
            child(KEPT, title="Publish a signed wheel"), "contract", id="milestone-title-edited"
        ),
        pytest.param(child(KEPT, target_date="2026-12-01"), "contract", id="milestone-date-edited"),
        pytest.param(
            child({BATCH_A: ((TASK_1, "CR-01"),), BATCH_B: ((TASK_2, "CR-01"), (TASK_3, "CR-01"))}),
            "Task EAWF-0002",
            id="task-moved",
        ),
        pytest.param(
            child({**KEPT, BATCH_A: ((TASK_1, "CR-02"), (TASK_2, "CR-01"))}),
            "Task EAWF-0001",
            id="task-criteria-edited",
        ),
        pytest.param(
            child({**KEPT, BATCH_A: ((TASK_1, "CR-01"), (TASK_2, "CR-02"))}),
            "Task EAWF-0002",
            id="planned-task-criteria-edited",
        ),
        pytest.param(
            child(
                {BATCH_A: ((TASK_1, "CR-01"),), BATCH_B: ((TASK_3, "CR-01"),)}, dropped=(TASK_2,)
            ),
            "drops Task EAWF-0002",
            id="task-dropped",
        ),
        pytest.param(
            child({**KEPT, BATCH_A: ((TASK_1, "CR-01"), (TASK_2, "CR-01"), (TASK_4, "CR-01"))}),
            "adds Task EAWF-0004 to Batch BAT-0001",
            id="task-added-to-an-existing-batch",
        ),
    ],
)
def test_an_edit_or_removal_on_an_active_milestone_is_refused(
    tmp_path: Path, body: dict[str, Any], clause: str
) -> None:
    """Anything but an append keeps the existing guard and writes nothing."""
    canary = active_parent(tmp_path, code="APEDIT")
    drive(canary, tmp_path, key=CHILD_KEY, body=body, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    error = answer["errors"][0]
    assert (error["code"], error["guard"]) == ("transition_guard_failed", GUARD)
    assert clause in error["message"]
    assert error["remediation"]
    assert document_path(canary).read_bytes() == before


def test_a_completed_task_restated_without_its_waiver_is_left_as_stored(tmp_path: Path) -> None:
    """A waiver the plan cannot carry does not make restating a COMPLETED Task an edit."""
    canary = active_parent(tmp_path, code="APWAIVE")
    criterion = collection(canary, "task")["EAWF-0002"]["criteria"][0]
    waived = [{**criterion, "waiver_reason": "measured by the release dry run instead"}]
    edit_row(canary, "task", "EAWF-0002", **COMPLETED, criteria=waived)
    tasks_before = collection(canary, "task")

    answer = apply_append(canary, tmp_path, child(KEPT))

    assert answer["status"] == "ok", answer["errors"]
    tasks = collection(canary, "task")
    assert tasks == tasks_before
    assert tasks["EAWF-0002"]["criteria"] == waived


def test_a_completed_task_restated_without_a_dropped_dependency_is_admitted(
    tmp_path: Path,
) -> None:
    """A dependency on a Task the plan drops is not restated, and the stored row keeps it."""
    parent = child({BATCH_A: KEPT[BATCH_A], BATCH_B: ((TASK_3, "CR-01"), (TASK_4, "CR-01"))})
    canary = active_parent(tmp_path, code="APDEPS", body=parent)
    edit_row(canary, "task", "EAWF-0004", status="CANCELLED")
    edit_row(canary, "task", "EAWF-0002", **COMPLETED, depends_on=[TASK_4])
    tasks_before = collection(canary, "task")

    answer = apply_append(canary, tmp_path, child(KEPT, dropped=(TASK_4,)))

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "task") == tasks_before
    assert tasks_before["EAWF-0002"]["depends_on"] == [TASK_4]


def test_a_completed_task_restated_under_another_batch_is_refused(tmp_path: Path) -> None:
    """A terminal Task is spared the content comparison, never the move check."""
    canary = active_parent(tmp_path, code="APMOVE")
    edit_row(canary, "task", "EAWF-0002", **COMPLETED)
    moved = child({BATCH_A: ((TASK_1, "CR-01"),), BATCH_B: ((TASK_2, "CR-01"), (TASK_3, "CR-01"))})
    drive(canary, tmp_path, key=CHILD_KEY, body=moved, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    error = answer["errors"][0]
    assert (error["code"], error["guard"]) == ("transition_guard_failed", GUARD)
    assert "Task EAWF-0002" in error["message"]
    assert document_path(canary).read_bytes() == before


def test_a_compacted_task_and_batch_restated_are_left_in_their_ledgers(
    tmp_path: Path,
) -> None:
    """A row in the ledger tier is placed: the append neither recreates nor rewrites it."""
    canary = active_parent(tmp_path, code="APLEDGER")
    edit_row(canary, "task", "EAWF-0003", **COMPLETED)
    edit_row(canary, "batch", "BAT-0002", status="COMPLETED")
    compact(canary, "task", "EAWF-0003")
    compact(canary, "batch", "BAT-0002")
    ledgers_before = ledger_bytes(canary)
    batches_before = collection(canary, "batch")
    tasks_before = collection(canary, "task")

    answer = apply_append(canary, tmp_path, child(KEPT))

    assert answer["status"] == "ok", answer["errors"]
    assert ledger_bytes(canary) == ledgers_before
    assert collection(canary, "batch") == batches_before
    assert collection(canary, "task") == tasks_before
    assert "BAT-0002" not in batches_before
    assert "EAWF-0003" not in tasks_before


def test_a_compacted_task_restated_under_another_batch_is_refused(tmp_path: Path) -> None:
    """A ledger-only Task keeps the move check its document-tier twin has."""
    canary = active_parent(tmp_path, code="APLEDMOVE")
    edit_row(canary, "task", "EAWF-0002", **COMPLETED)
    compact(canary, "task", "EAWF-0002")
    moved = child({BATCH_A: ((TASK_1, "CR-01"),), BATCH_B: ((TASK_2, "CR-01"), (TASK_3, "CR-01"))})
    drive(canary, tmp_path, key=CHILD_KEY, body=moved, parent_key=PARENT_KEY, apply=False)
    before = document_path(canary).read_bytes()
    ledgers_before = ledger_bytes(canary)

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    error = answer["errors"][0]
    assert (error["code"], error["guard"]) == ("transition_guard_failed", GUARD)
    assert "Task EAWF-0002" in error["message"]
    assert document_path(canary).read_bytes() == before
    assert ledger_bytes(canary) == ledgers_before


@pytest.mark.parametrize("compacted", [False, True], ids=["document-tier", "ledger-tier"])
def test_a_dropped_task_in_dropped_tasks_is_admitted(tmp_path: Path, compacted: bool) -> None:
    """A DROPPED Task is as droppable as a CANCELLED one, in either tier."""
    parent = child({BATCH_A: KEPT[BATCH_A], BATCH_B: ((TASK_3, "CR-01"), (TASK_4, "CR-01"))})
    canary = active_parent(tmp_path, code="APDROP", body=parent)
    edit_row(canary, "task", "EAWF-0004", **DROPPED)
    if compacted:
        compact(canary, "task", "EAWF-0004")
    tasks_before = collection(canary, "task")
    ledgers_before = ledger_bytes(canary)

    answer = apply_append(canary, tmp_path, child(KEPT, dropped=(TASK_4,)))

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "task") == tasks_before
    assert ledger_bytes(canary) == ledgers_before
    assert ("EAWF-0004" in tasks_before) is not compacted


def claiming(body: dict[str, Any], claims: dict[str, str]) -> dict[str, Any]:
    """Return *body* with each named Task URN claiming its one write path."""
    for task in body["tasks"]:
        if task["urn"] in claims:
            task["write_claims"] = [claims[task["urn"]]]
    return body


def overlapping_parent(tmp_path: Path, *, code: str) -> CanaryProvision:
    """Return an ACTIVE parent whose two in-flight Tasks claim one path, and whose third is done.

    EAWF-0001 (CLAIMED) and EAWF-0003 (PLANNED) both claim ``src/shared.py``,
    unordered, as though placed before the lens judged it; EAWF-0002 is
    COMPLETED with a claim on ``src/done.py`` and compacted into the ledger.
    """
    canary = active_parent(tmp_path, code=code)
    edit_row(canary, "task", "EAWF-0001", write_claims=["src/shared.py"])
    edit_row(canary, "task", "EAWF-0003", write_claims=["src/shared.py"])
    edit_row(canary, "task", "EAWF-0002", **COMPLETED, write_claims=["src/done.py"])
    compact(canary, "task", "EAWF-0002")
    return canary


def test_an_append_judges_write_claim_overlap_only_for_its_new_tasks(tmp_path: Path) -> None:
    """Restated and finished Tasks are not re-judged; a new Task on a done path is admitted."""
    canary = overlapping_parent(tmp_path, code="APCLAIM")
    body = claiming(
        child({**KEPT, BATCH_C: ((TASK_4, "CR-01"),)}),
        {
            TASK_1: "src/shared.py",
            TASK_2: "src/done.py",
            TASK_3: "src/shared.py",
            TASK_4: "src/done.py",
        },
    )

    answer = apply_append(canary, tmp_path, body)

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "task")["EAWF-0004"]["write_claims"] == ["src/done.py"]


def test_a_new_task_overlapping_an_in_flight_task_is_refused_at_submit(tmp_path: Path) -> None:
    """A new Task sharing an in-flight Task's path with no edge still refuses."""
    canary = overlapping_parent(tmp_path, code="APCLASH")
    body = claiming(
        child({**KEPT, BATCH_C: ((TASK_4, "CR-01"),)}),
        {TASK_1: "src/shared.py", TASK_3: "src/shared.py", TASK_4: "src/shared.py"},
    )
    before = document_path(canary).read_bytes()

    answer = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {
                "key": CHILD_KEY,
                "author": OPERATOR,
                "body": body,
                "parent_key": PARENT_KEY,
            },
            "actor": ACTOR,
            "idempotency_key": "submit-clash",
        },
    )

    assert answer["status"] == "error"
    error = answer["errors"][0]
    assert error["guard"] == "plan_lens_ownership"
    assert f"{TASK_1} and {TASK_4}" in error["message"]
    assert f"{TASK_3} and {TASK_4}" in error["message"]
    assert f"{TASK_1} and {TASK_3}" not in error["message"]
    assert document_path(canary).read_bytes() == before


def test_appending_a_batch_of_another_milestone_is_refused(tmp_path: Path) -> None:
    """Another Milestone's Batch is a key the plan does not own, so it stays claimed."""
    canary = active_parent(tmp_path, code="APFOREIGN")
    other = plan_body(batches={BATCH_C: ((TASK_4, "CR-01"),)}, milestone_urn=OTHER_MILESTONE_URN)
    assert drive(canary, tmp_path, key=OTHER_KEY, body=other)["status"] == "ok"
    drive(
        canary,
        tmp_path,
        key=CHILD_KEY,
        body=child({**KEPT, BATCH_C: ((TASK_4, "CR-01"),)}),
        parent_key=PARENT_KEY,
        apply=False,
    )
    before = document_path(canary).read_bytes()

    answer = apply_plan_key(canary, tmp_path, key=CHILD_KEY)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "plan_keys_unclaimed"
    assert "batch/BAT-0003" in answer["errors"][0]["message"]
    assert "milestone/MLS-0030" not in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_a_duplicate_required_batch_ref_is_refused_at_submission(tmp_path: Path) -> None:
    """Appending a Batch the Milestone already requires never reaches the apply."""
    canary = active_parent(tmp_path, code="APDUP")
    before = document_path(canary).read_bytes()

    answer = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        params={
            "proposal": {
                "key": CHILD_KEY,
                "author": OPERATOR,
                "body": child(KEPT, required=(BATCH_A, BATCH_B, BATCH_B)),
                "parent_key": PARENT_KEY,
            },
            "actor": ACTOR,
            "idempotency_key": "submit-duplicate",
        },
    )

    assert answer["status"] == "error"
    assert "proposal.body.milestone" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_a_planned_milestone_still_re_plans_by_rewriting_and_cancelling(tmp_path: Path) -> None:
    """The PLANNED path is unchanged: it rewrites the contract and cancels what it drops."""
    canary = planned_canary(tmp_path, code="APPLANNED")
    seed(canary, {"project": {"EAWF": IMPORTED_PROJECT_ROW}})
    assert drive(canary, tmp_path, key=PARENT_KEY, body=PARENT_BODY)["status"] == "ok"

    answer = apply_child(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    milestone = collection(canary, "milestone")["MLS-0030"]
    assert (milestone["status"], milestone["title"]) == ("PLANNED", "Publish a signed wheel")
    assert milestone["required_batch_refs"] == [BATCH_C]
    batches = collection(canary, "batch")
    assert [batches[key]["status"] for key in ("BAT-0001", "BAT-0002", "BAT-0003")] == [
        "CANCELLED",
        "CANCELLED",
        "PLANNED",
    ]
