"""The active-Batch ceiling counts only Batches holding or building an exact-head proof.

A Batch that pinned its head (READY_TO_MERGE through MERGED_PENDING_RECONCILIATION), or an
ACTIVE one an integration generation is filed for, holds its repository's slot; an ACTIVE
Batch still collecting Tasks does not, so a ready, landed Batch beside it closes. Each close
runs over a real canary and git repository; the console's waiting note is read off the same
rows the projection builds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.delivery.integration import IntegrationGeneration
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.row_facts import BATCH_INTEGRATING_FACT
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.state.epoch2.batch import BatchStatus, holds_head_proof
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.batch_close import close_landed_batches
from eawf.runtime.daemon.methods.delivery import _append_generation
from eawf.runtime.daemon.methods.domain_guards import integrating_batch_refs
from eawf.surfaces.tui.console.renderers.task_progress import Progress
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    document_path,
    rekeyed,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_batch_close import (
    ACTOR,
    SECOND_BATCH_URN,
    batch,
    commit,
    context_of,
    ledger_status,
    live,
    make_tree,
    task,
)
from tests.integration.workflow.delivery._completion_fixtures import (
    BASE,
    BASE_COMMIT,
    generation,
)
from tests.tui.surfaces.tui.console.test_task_progress import ROOT, WAITING, document

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
OTHER_REPOSITORY: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/repository/REP-OTHER"
OTHER_TRACK: Final = f"{SLOT}/track/TRK-OTHER"

#: The states in which a Batch has pinned the head it was checked at, short of COMPLETED.
PINNED: Final = ("READY_TO_MERGE", "MERGING", "MERGED_PENDING_RECONCILIATION")


def file_generation(canary: CanaryProvision, runtime: Path, urn: str) -> None:
    """File one integration generation for the Batch *urn*, where ``batch integrate`` does."""
    filed = generation(ordinal=2, head_sha=BASE_COMMIT, target_base=BASE, tasks=())
    # every binding the generation carries names its Batch, so all of them are rebound
    rebound = filed.model_dump_json().replace(str(filed.batch_ref), urn)
    with root_context(canary, runtime).session([urn]) as session:
        _append_generation(session, IntegrationGeneration.model_validate_json(rebound))


def holder(status: str, **overrides: Any) -> dict[str, Any]:
    """Return BAT-0007 in *status*, listing EAWF-0042, as the Batch that may hold the slot."""
    row = batch("BAT-0007", status, ["EAWF-0042"])
    row.update(overrides)
    return row


def world(tmp_path: Path, first: dict[str, Any], *, first_task: str) -> CanaryProvision:
    """Seed *first* beside BAT-0008, a PLANNED Batch whose one Task is ready and landed."""
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": first, "BAT-0008": batch("BAT-0008", "PLANNED", ["EAWF-0043"])},
        tasks={
            "EAWF-0042": task("EAWF-0042", first_task),
            "EAWF-0043": task("EAWF-0043", "READY_TO_INTEGRATE", batch=SECOND_BATCH_URN),
        },
    )
    commit(repo, "src/second.py", "feat: second\n\nTask: EAWF-0043")
    return canary


def close_second(canary: CanaryProvision, tmp_path: Path) -> tuple[bool, str]:
    """Close BAT-0008 alone and return whether it closed and why."""
    closures, _ = close_landed_batches(
        context_of(canary, tmp_path), ("BAT-0008",), actor=ACTOR, now=AT
    )
    (closure,) = closures
    return closure.closed, closure.reason


# ---- (a) an ACTIVE Batch collecting Tasks holds no slot ------------------------------


def test_a_ready_landed_batch_closes_beside_an_active_one_collecting_tasks(
    tmp_path: Path,
) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")

    closures, _ = close_landed_batches(context_of(canary, tmp_path), (), actor=ACTOR, now=AT)

    assert [(item.batch_key, item.closed) for item in closures] == [
        ("BAT-0007", False),
        ("BAT-0008", True),
    ]
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0008") == "COMPLETED"
    assert live(canary, Epoch2Collection.BATCH)["BAT-0007"]["status"] == "ACTIVE"


def test_two_ready_batches_close_one_after_the_other_beside_a_collecting_one(
    tmp_path: Path,
) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")
    third = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0009"
    seed(
        canary,
        {
            "batch": {"BAT-0009": batch("BAT-0009", "PLANNED", ["EAWF-0044"])},
            "task": {"EAWF-0044": task("EAWF-0044", "READY_TO_INTEGRATE", batch=third)},
        },
    )

    closures, _ = close_landed_batches(
        context_of(canary, tmp_path), ("BAT-0008", "BAT-0009"), actor=ACTOR, now=AT
    )

    assert [item.closed for item in closures] == [True, True]


# ---- (b) a Batch holding or building a proof still refuses activation -----------------


@pytest.mark.parametrize("status", PINNED)
def test_a_batch_on_its_pinned_head_keeps_a_ready_one_planned(tmp_path: Path, status: str) -> None:
    canary = world(tmp_path, holder(status), first_task="READY_TO_INTEGRATE")

    closed, reason = close_second(canary, tmp_path)

    assert not closed
    assert "batch_wip_admits" in reason
    assert live(canary, Epoch2Collection.BATCH)["BAT-0008"]["status"] == "PLANNED"


def test_an_active_integrating_batch_keeps_a_ready_one_planned(tmp_path: Path) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="RUNNING")
    file_generation(canary, tmp_path / "runtime", BATCH_URN)

    closed, reason = close_second(canary, tmp_path)

    assert not closed
    assert "batch_wip_admits" in reason


def test_an_unreadable_batch_ledger_refuses_beside_an_active_batch(tmp_path: Path) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")
    ledger = ledger_path(document_path(canary), Epoch2Collection.BATCH)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{not a ledger line", encoding="utf-8")

    closed, reason = close_second(canary, tmp_path)

    assert not closed
    assert "batch_wip_admits" in reason


# ---- (c) other repositories and other Tracks are not counted --------------------------


@pytest.mark.parametrize("status", ["READY_TO_MERGE", "ACTIVE"])
def test_a_proof_in_another_repository_is_not_counted(tmp_path: Path, status: str) -> None:
    canary = world(
        tmp_path, holder(status, repository_ref=OTHER_REPOSITORY), first_task="READY_TO_INTEGRATE"
    )
    file_generation(canary, tmp_path / "runtime", BATCH_URN)

    assert close_second(canary, tmp_path)[0]


@pytest.mark.parametrize("status", ["READY_TO_MERGE", "ACTIVE"])
def test_a_proof_of_another_track_is_not_counted(tmp_path: Path, status: str) -> None:
    other = rekeyed(seed_row("milestone", "ACTIVE"), key="MLS-0031")
    other["primary_track_ref"] = OTHER_TRACK
    canary = world(
        tmp_path,
        holder(status, milestone_ref=f"{SLOT}/milestone/MLS-0031"),
        first_task="READY_TO_INTEGRATE",
    )
    seed(
        canary,
        {
            "track": {"TRK-OTHER": rekeyed(seed_row("track", "ACTIVE"), key="TRK-OTHER")},
            "milestone": {"MLS-0031": other},
        },
    )
    file_generation(canary, tmp_path / "runtime", BATCH_URN)

    assert close_second(canary, tmp_path)[0]


# ---- the shared predicate and the ledger read -----------------------------------------


@pytest.mark.parametrize(
    ("status", "integrating", "holds"),
    [
        *((state, False, True) for state in PINNED),
        ("ACTIVE", True, True),
        ("ACTIVE", False, False),
        ("PLANNED", True, False),
        ("COMPLETED", False, False),
        ("CANCELLED", False, False),
        ("FAILED", False, False),
        (None, True, False),
        ("", False, False),
    ],
)
def test_holds_head_proof_counts_pinned_and_integrating_batches_only(
    status: str | None, integrating: bool, holds: bool
) -> None:
    assert holds_head_proof(status, integrating=integrating) is holds


def test_holds_head_proof_covers_every_stored_status() -> None:
    held = {state.value for state in BatchStatus if holds_head_proof(state.value, integrating=True)}
    assert held == {"ACTIVE", *PINNED}


def test_integrating_batch_refs_reads_an_absent_ledger_as_none_integrating(tmp_path: Path) -> None:
    assert integrating_batch_refs({}, tmp_path / "state.json") == frozenset()


def test_integrating_batch_refs_names_each_batch_a_generation_is_filed_for(
    tmp_path: Path,
) -> None:
    canary = world(tmp_path, holder("ACTIVE"), first_task="PLANNED")
    file_generation(canary, tmp_path / "runtime", BATCH_URN)

    path = document_path(canary)

    assert integrating_batch_refs(read_document(path), path) == frozenset({BATCH_URN})


# ---- (d) the console's waiting note agrees with the ceiling ---------------------------


def _waiting(doc: dict[str, Any], integrating: frozenset[str]) -> str | None:
    projection = build_route_projection(
        route="scope.home",
        document=doc,
        cursor=1,
        scope_id="EAWF",
        generated_at=AT,
        integrating=integrating,
    )
    spine = build_spine_view(projection)
    row = next(row for row in spine.rows if row.key == "BAT-0101")
    filed = Progress.of(spine.rows)
    return filed.waiting(row, filed.batch(row.key))


def test_no_waiting_note_beside_an_active_batch_collecting_tasks() -> None:
    assert _waiting(document(), frozenset()) is None


def test_the_waiting_note_beside_an_active_integrating_batch() -> None:
    integrating = frozenset({f"{ROOT}/batch/BAT-0100"})
    assert _waiting(document(), integrating) == WAITING


@pytest.mark.parametrize("status", PINNED)
def test_the_waiting_note_beside_a_batch_on_its_pinned_head(status: str) -> None:
    doc = document()
    doc["batch"]["BAT-0100"]["status"] = status
    assert _waiting(doc, frozenset()) == WAITING


def test_the_integrating_fact_travels_only_on_an_integrating_batch() -> None:
    projection = build_route_projection(
        route="scope.home",
        document=document(),
        cursor=1,
        scope_id="EAWF",
        generated_at=AT,
        integrating=frozenset({f"{ROOT}/batch/BAT-0100"}),
    )
    facts = {row.key: row.facts for row in projection.rows if row.key.startswith("BAT-")}
    assert facts["BAT-0100"][BATCH_INTEGRATING_FACT] == "yes"
    assert BATCH_INTEGRATING_FACT not in facts["BAT-0101"]
