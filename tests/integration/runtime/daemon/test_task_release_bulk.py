"""A claimed Task's lease is released by its holder, alone or in one bulk operation.

``domain.task.release`` moves a CLAIMED Task back to PLANNED, files a release record
naming its cause and actor, and refuses a principal that does not hold the claim or a
Task a Run is still open on. ``runtime.bulk.*`` runs the same verb over many Tasks as one
operation with one result per Task. Every call runs through the live daemon verbs against
a provisioned epoch-2 canary.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.state.epoch2.task import TaskReleaseRecord
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.bulk import (
    BULK_CONTROL_METHOD,
    BULK_PREVIEW_METHOD,
    BULK_RECONCILE_METHOD,
    BULK_RELEASE_CAUSE,
)
from eawf.runtime.daemon.task_release import TASK_RELEASE_METHOD, release_record_key
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
OTHER: Final = "OP-0002"
_TASKS: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task"
_RUNS: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run"
#: Held by the actor, held by nobody recorded, held by another principal, worked by a
#: queued Run, moved past the revision it is confirmed at, and absent.
MINE, UNRECORDED, THEIRS, BUSY, MOVED, MISSING = (
    f"{_TASKS}/EAWF-00{n}" for n in (51, 52, 53, 54, 55, 59)
)


def _key(urn: str) -> str:
    return urn.rsplit("/", 1)[1]


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _claimed(urn: str, *, holder: str | None, revision: int = 1) -> dict[str, Any]:
    row = rekeyed(seed_row("task", "CLAIMED"), key=_key(urn))
    row["revision"] = revision
    if holder is not None:
        row["claimed_by"] = holder
    return row


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding five CLAIMED Tasks and a Run queued against one of them."""
    provisioned = provision(tmp_path / "repo", code="REL")
    tasks = {
        _key(MINE): _claimed(MINE, holder=ACTOR),
        _key(UNRECORDED): _claimed(UNRECORDED, holder=None),
        _key(THEIRS): _claimed(THEIRS, holder=OTHER),
        _key(BUSY): _claimed(BUSY, holder=ACTOR),
        _key(MOVED): _claimed(MOVED, holder=ACTOR, revision=2),
    }
    run = seed_row("run", "QUEUED")
    run["scope"]["task_ref"] = BUSY
    seed(provisioned, {"task": tasks, "run": {"RUN-00000010": run}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(ctx: MethodContext, canary: CanaryProvision, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb against the canary the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def release(
    ctx: MethodContext, canary: CanaryProvision, urn: str, *, actor: str = ACTOR, key: str = "rel-1"
) -> dict[str, Any]:
    """Release one Task's lease at revision 1."""
    return call(
        ctx,
        canary,
        TASK_RELEASE_METHOD,
        urn=urn,
        expected_revision=1,
        idempotency_key=key,
        actor=actor,
        reason_code="operator-abort",
    )


def bulk_body(
    ctx: MethodContext, canary: CanaryProvision, items: list[str], *, key: str = "bulk-rel"
) -> dict[str, Any]:
    """Return the opening request of a bulk release over *items*, anchored at revision 1."""
    shown = call(ctx, canary, BULK_PREVIEW_METHOD, verb="release", item_refs=items)
    return {
        "verb": "release",
        "item_refs": items,
        "expected_revisions": dict.fromkeys(items, 1),
        "actor": ACTOR,
        "idempotency_key": key,
        "confirmation_digest": shown["confirmation_digest"],
    }


def document(canary: CanaryProvision) -> dict[str, Any]:
    """Return the canary's current document."""
    return read_document(document_path(canary))


def release_lines(canary: CanaryProvision, tmp_path: Path) -> list[TaskReleaseRecord]:
    """Return every release record the Task ledger holds, in ledger order."""
    context = root_context(canary, tmp_path / "reader")
    with context.session([parse_qualified_urn(MINE)]) as session:
        lines = read_ledger_records(session.ledger_path(Epoch2Collection.TASK))
    return [
        TaskReleaseRecord.model_validate(line.payload)
        for line in lines
        if line.payload.get("payload_kind") == "task_release"
    ]


def states(operation: dict[str, Any]) -> dict[str, str]:
    """Return each item's state, keyed by URN."""
    return {urn: row["state"] for urn, row in operation["item_results"].items()}


# ---- domain.task.release -----------------------------------------------------


def test_del_024_release_plans_the_task_again_and_files_its_record(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = release(ctx, canary, MINE)

    assert answer["status"] == "ok"
    assert answer["result"]["event_name"] == "domain.task.lease_released"
    row = document(canary)["task"][_key(MINE)]
    assert row["status"] == "PLANNED"
    assert "claimed_by" not in row
    assert row["contract_revision"] == 2
    [record] = release_lines(canary, tmp_path)
    assert record.cause == "operator-abort"
    assert record.actor == ACTOR
    assert (record.prior_revision, record.new_revision) == (1, 2)
    assert str(record.target_batch_ref) == row["batch_ref"]


def test_del_021_release_by_a_principal_not_holding_the_claim_is_refused(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    before = document_path(canary).read_bytes()

    answer = release(ctx, canary, THEIRS)

    assert answer["status"] == "error"
    assert answer["errors"][0]["guard"] == "releaser_holds_lease"
    assert document_path(canary).read_bytes() == before
    assert release_lines(canary, tmp_path) == []


def test_del_021_a_claim_with_no_recorded_holder_is_released_by_any_principal(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    answer = release(ctx, canary, UNRECORDED, actor=OTHER)

    assert answer["status"] == "ok"


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "SUSPENDED"])
def test_del_021_release_while_a_run_is_open_on_the_task_is_refused(
    ctx: MethodContext, canary: CanaryProvision, status: str
) -> None:
    run = seed_row("run", status)
    run["scope"]["task_ref"] = MINE
    seed(canary, {"run": {"RUN-00000011": rekeyed(run, key="RUN-00000011")}})

    answer = release(ctx, canary, MINE)

    assert answer["errors"][0]["guard"] == "no_active_run"
    assert document(canary)["task"][_key(MINE)]["status"] == "CLAIMED"


def test_del_021_a_finished_run_does_not_hold_the_task(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    run = seed_row("run", "COMPLETED")
    run["scope"]["task_ref"] = MINE
    seed(canary, {"run": {"RUN-00000011": rekeyed(run, key="RUN-00000011")}})

    assert release(ctx, canary, MINE)["status"] == "ok"


def test_del_021_release_of_a_task_that_is_not_claimed_is_an_illegal_transition(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    release(ctx, canary, MINE)

    answer = call(
        ctx,
        canary,
        TASK_RELEASE_METHOD,
        urn=MINE,
        expected_revision=2,
        idempotency_key="rel-2",
        actor=ACTOR,
        reason_code="operator-abort",
    )

    assert answer["errors"][0]["code"] == "illegal_transition"


def test_del_021_release_without_a_cause_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    answer = call(
        ctx,
        canary,
        TASK_RELEASE_METHOD,
        urn=MINE,
        expected_revision=1,
        idempotency_key="rel-1",
        actor=ACTOR,
    )

    assert answer["errors"][0]["guard"] == "reason_recorded"


def test_del_025_a_replayed_release_files_its_record_once(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    first = release(ctx, canary, MINE)
    again = release(ctx, canary, MINE)

    assert again["result"] == first["result"]
    assert len(release_lines(canary, tmp_path)) == 1


def test_del_023_a_release_record_lost_to_a_crash_is_filed_on_the_retry(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the daemon died after the commit")

    monkeypatch.setattr("eawf.runtime.daemon.methods.domain.file_task_release", crash)
    with pytest.raises(OSError, match="died after the commit"):
        release(ctx, canary, MINE)
    monkeypatch.undo()
    assert release_lines(canary, tmp_path) == []

    assert release(ctx, canary, MINE)["status"] == "ok"
    [record] = release_lines(canary, tmp_path)
    assert record.new_revision == 2


def test_del_021_a_claim_records_who_holds_the_lease(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    release(ctx, canary, MINE)

    claimed = call(
        ctx,
        canary,
        "domain.task.claim",
        urn=MINE,
        expected_revision=2,
        idempotency_key="claim-2",
        actor=OTHER,
    )

    assert claimed["status"] == "ok"
    assert document(canary)["task"][_key(MINE)]["claimed_by"] == OTHER
    refused = call(
        ctx,
        canary,
        TASK_RELEASE_METHOD,
        urn=MINE,
        expected_revision=3,
        idempotency_key="rel-3",
        actor=ACTOR,
        reason_code="operator-abort",
    )
    assert refused["errors"][0]["guard"] == "releaser_holds_lease"


# ---- runtime.bulk.* over Tasks -------------------------------------------------


def test_del_021_a_bulk_release_judges_every_task_on_its_own(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    items = [MINE, UNRECORDED, THEIRS, BUSY, MOVED, MISSING]

    operation = call(ctx, canary, BULK_CONTROL_METHOD, **bulk_body(ctx, canary, items))

    results = operation["item_results"]
    assert states(operation) == {
        MINE: "confirmed",
        UNRECORDED: "confirmed",
        THEIRS: "rejected",
        BUSY: "rejected",
        MOVED: "rejected",
        MISSING: "rejected",
    }
    assert results[THEIRS]["code"] == "releaser_holds_lease"
    assert results[BUSY]["code"] == "no_active_run"
    assert results[MOVED]["code"] == "revision_conflict"
    assert results[MISSING]["code"] == "identity_not_found"
    tasks = document(canary)["task"]
    assert {key: tasks[_key(key)]["status"] for key in (MINE, THEIRS, BUSY)} == {
        MINE: "PLANNED",
        THEIRS: "CLAIMED",
        BUSY: "CLAIMED",
    }


def test_del_021_a_bulk_release_records_its_own_cause(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    call(ctx, canary, BULK_CONTROL_METHOD, **bulk_body(ctx, canary, [MINE]))

    [record] = release_lines(canary, tmp_path)
    assert record.cause == BULK_RELEASE_CAUSE
    assert record.actor == ACTOR


def test_del_021_a_run_is_not_a_release_item(ctx: MethodContext, canary: CanaryProvision) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(ctx, canary, BULK_PREVIEW_METHOD, verb="release", item_refs=[f"{_RUNS}/RUN-00000010"])


def test_del_022_a_partial_bulk_release_is_a_normal_answer(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    operation = call(
        ctx, canary, BULK_CONTROL_METHOD, **bulk_body(ctx, canary, [MINE, UNRECORDED, THEIRS])
    )

    assert operation["aggregate"] == {
        "requested": 0,
        "accepted": 0,
        "rejected": 1,
        "confirmed": 2,
        "unknown": 0,
        "invalidated": 0,
    }
    assert "success" not in operation


def test_del_023_a_release_whose_answer_was_lost_stays_unknown_until_reconciled(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = bulk_body(ctx, canary, [MINE, UNRECORDED])
    real = methods.dispatch

    async def lost(name: str, context: MethodContext, params: dict[str, Any]) -> Any:
        if name == TASK_RELEASE_METHOD and params["urn"] == UNRECORDED:
            raise OSError("the answer never came back")
        return await real(name, context, params)

    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", lost)
    opened = call(ctx, canary, BULK_CONTROL_METHOD, **body)
    still = call(ctx, canary, BULK_RECONCILE_METHOD, **body)
    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", real)
    reconciled = call(ctx, canary, BULK_RECONCILE_METHOD, **body)

    assert states(opened) == {MINE: "confirmed", UNRECORDED: "unknown"}
    assert states(still) == {MINE: "confirmed", UNRECORDED: "unknown"}
    assert document(canary)["task"][_key(UNRECORDED)]["status"] == "PLANNED"
    assert states(reconciled) == {MINE: "confirmed", UNRECORDED: "confirmed"}


def test_del_023_reconciling_a_task_that_moved_meanwhile_invalidates_it(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = bulk_body(ctx, canary, [UNRECORDED])
    real = methods.dispatch

    async def lost(name: str, context: MethodContext, params: dict[str, Any]) -> Any:
        raise OSError("the answer never came back")

    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", lost)
    opened = call(ctx, canary, BULK_CONTROL_METHOD, **body)
    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", real)
    release(ctx, canary, UNRECORDED, key="someone-else")

    reconciled = call(ctx, canary, BULK_RECONCILE_METHOD, **body)

    assert states(opened) == {UNRECORDED: "unknown"}
    assert states(reconciled) == {UNRECORDED: "invalidated"}


def test_del_024_the_release_preview_names_count_effects_non_effects_and_rule(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    before = document(canary)

    shown = call(ctx, canary, BULK_PREVIEW_METHOD, verb="release", item_refs=[THEIRS, MINE])

    confirmation = shown["confirmation"]
    assert confirmation["target_count"] == 2
    assert confirmation["item_refs"] == [MINE, THEIRS]
    assert any("PLANNED" in line for line in confirmation["effects"])
    assert any("does not cancel" in line for line in confirmation["non_effects"])
    assert "rejected on its own" in confirmation["invalidation_rule"]
    assert shown["expected_revisions"] == {MINE: 1, THEIRS: 1}
    assert document(canary) == before


def test_del_024_cancelling_a_run_does_not_release_its_task(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    run = f"{_RUNS}/RUN-00000010"
    shown = call(ctx, canary, BULK_PREVIEW_METHOD, verb="cancel", item_refs=[run])
    call(
        ctx,
        canary,
        BULK_CONTROL_METHOD,
        verb="cancel",
        item_refs=[run],
        expected_revisions={run: 1},
        actor=ACTOR,
        idempotency_key="bulk-cancel",
        confirmation_digest=shown["confirmation_digest"],
    )

    assert document(canary)["task"][_key(BUSY)]["status"] == "CLAIMED"


def test_del_025_a_replayed_bulk_release_returns_the_original_results(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    body = bulk_body(ctx, canary, [MINE, THEIRS])
    opened = call(ctx, canary, BULK_CONTROL_METHOD, **body)

    replayed = call(ctx, canary, BULK_CONTROL_METHOD, **{**body, "item_refs": [THEIRS, MINE]})

    assert replayed == opened
    assert len(release_lines(canary, tmp_path)) == 1


def test_del_025_the_same_key_over_other_tasks_is_an_idempotency_conflict(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    call(ctx, canary, BULK_CONTROL_METHOD, **bulk_body(ctx, canary, [MINE, THEIRS]))

    with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
        call(ctx, canary, BULK_CONTROL_METHOD, **bulk_body(ctx, canary, [MINE]))


def test_del_025_the_release_record_key_is_one_per_release() -> None:
    urn = parse_qualified_urn(MINE)

    assert release_record_key(urn, 2) == "TRL-EAWF-0051-r2"
    assert release_record_key(urn, 2) != release_record_key(urn, 4)
