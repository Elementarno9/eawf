"""Each Task's candidate is its last seal in ledger order.

Assembly, integration and completion all read the run ledger for the
candidate a Task stands on. They select it through one helper, so a Task
that sealed again after a repair is integrated and judged on its newest
tree while the older seal stays recorded, untouched, as history.

Nothing sleeps, polls or reaches outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.runtime.candidate import CandidateBundle
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.store.ledger import LedgerRecord
from eawf.runtime.daemon.methods.delivery import latest_sealed_bundles
from eawf.runtime.daemon.methods.delivery_completion import (
    TaskCompletionParams,
    assess_task_completion,
    sealed_bundle_of,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

FOREIGN_TASK: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-SIDE/task/EAWF-0042"


def seal(
    *, task_ref: str = world.TASK, tree: str = "a", base: str = world.BASE_COMMIT
) -> CandidateBundle:
    """Return one sealed bundle of *task_ref* produced from *base*."""
    return world.bundle(task_ref=task_ref, tree=tree).model_copy(update={"base_commit": base})


def line(bundle: CandidateBundle) -> LedgerRecord:
    """Return the run-ledger line *bundle* is filed as."""
    return world.bundle_line(bundle.model_dump(mode="json"))


def unsealed(kind: str, *, task_ref: str = world.TASK) -> LedgerRecord:
    """Return a run-ledger line of another payload kind naming *task_ref*."""
    return LedgerRecord(
        collection=world.bundle_line(world.bundle_row()).collection,
        record_key=f"{kind}-0001",
        status="recorded",
        recorded_at=world.AT,
        payload={"payload_kind": kind, "task_ref": task_ref, "candidate_ref": "CND-later"},
    )


def test_latest_sealed_bundles_of_an_empty_ledger_selects_nothing() -> None:
    """Boundary: a ledger with no line has no candidate for any Task."""
    assert latest_sealed_bundles(()) == {}


def test_latest_sealed_bundles_selects_a_single_seal() -> None:
    """Boundary: one seal is its Task's candidate."""
    only = seal()
    assert latest_sealed_bundles((line(only),)) == {world.TASK: only}


def test_latest_sealed_bundles_replaces_an_older_seal_from_another_base() -> None:
    """A newer same-Task seal wins even though it started from another commit."""
    older = seal(tree="a", base=world.LOSING_HEAD)
    newer = seal(tree="c")
    assert latest_sealed_bundles((line(older), line(newer))) == {world.TASK: newer}


def test_latest_sealed_bundles_selects_each_task_independently() -> None:
    """Another Task's seal neither replaces nor is replaced, and order is the ledger's."""
    first = seal(tree="a")
    other = seal(task_ref=world.OTHER_TASK, tree="d")
    last = seal(tree="c")
    selected = latest_sealed_bundles((line(first), line(other), line(last)))
    assert list(selected.items()) == [(world.OTHER_TASK, other), (world.TASK, last)]


def test_latest_sealed_bundles_keys_by_the_fully_qualified_task() -> None:
    """Two Tasks sharing a key in different containers are different Tasks."""
    here = seal()
    there = seal(task_ref=FOREIGN_TASK, tree="d")
    assert latest_sealed_bundles((line(here), line(there))) == {
        world.TASK: here,
        FOREIGN_TASK: there,
    }


@pytest.mark.parametrize("kind", ["candidate_submission", "candidate_report_binding"])
def test_latest_sealed_bundles_ignores_an_unsealed_line_after_the_seal(kind: str) -> None:
    """A submission or binding filed later never supersedes the seal."""
    sealed = seal()
    assert latest_sealed_bundles((line(sealed), unsealed(kind))) == {world.TASK: sealed}


def test_latest_sealed_bundles_leaves_every_line_as_recorded() -> None:
    """Selection reads the ledger; the older seal stays exactly as it was filed."""
    records = (line(seal(tree="a", base=world.LOSING_HEAD)), line(seal(tree="c")))
    before = [item.model_dump(mode="json") for item in records]
    latest_sealed_bundles(records)
    assert [item.model_dump(mode="json") for item in records] == before


def test_latest_sealed_bundles_refuses_a_corrupt_bundle_line() -> None:
    """Error path: a line claiming to be a bundle that does not validate is corruption."""
    corrupt = LedgerRecord(
        collection=line(seal()).collection,
        record_key="CND-corrupt",
        status="sealed",
        recorded_at=world.AT,
        payload={"payload_kind": "candidate_bundle", "task_ref": world.TASK},
    )
    with pytest.raises(ValidationError):
        latest_sealed_bundles((corrupt,))


def test_sealed_bundle_of_reads_the_shared_selection() -> None:
    """Completion and proof see the same candidate integration does."""
    records = (line(seal(tree="a", base=world.LOSING_HEAD)), line(seal(tree="c")))
    assert (
        sealed_bundle_of(records, task_ref=world.bundle().task_ref)
        == (latest_sealed_bundles(records)[world.TASK])
    )
    assert sealed_bundle_of((), task_ref=world.bundle().task_ref) is None


def _completion_params(receipts: tuple[ProofReceipt, ...]) -> TaskCompletionParams:
    return TaskCompletionParams.model_validate(
        {
            "urn": world.TASK,
            "actor": "OPERATOR-LOCAL",
            "base": world.BASE,
            "report_verdict": AgentReportVerdict.PASS,
            "gates": (world.gate("CR-01"), world.gate("CR-02")),
            "receipts": receipts,
            "proof_facts": world.facts(),
        }
    )


def test_old_proof_receipts_do_not_complete_a_resealed_task(tmp_path: Path) -> None:
    """A reseal delivered on a new head needs fresh proof at that exact revision."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    delivered = world.delivering_generation()
    redelivered = world.generation(
        ordinal=3,
        head_sha=world.THIRD_HEAD,
        target_base=delivered.integrated_revision,
        tasks=(world.TASK,),
        affected=("CR-01", "CR-02"),
    )
    world.seed_lines(context, world.TASK, [line(seal(tree="a")), line(seal(tree="c"))])
    world.seed_lines(
        context,
        world.BATCH,
        [world.generation_line(delivered), world.generation_line(redelivered)],
    )
    contracts = world.compiled("CR-01", "CR-02")

    stale = assess_task_completion(
        context,
        _completion_params(
            world.receipts_at(contracts, revision_binding=delivered.integrated_revision)
        ),
    )
    fresh = assess_task_completion(
        context,
        _completion_params(
            world.receipts_at(contracts, revision_binding=redelivered.integrated_revision)
        ),
    )

    assert not stale.completable
    assert fresh.completable
    assert fresh.head_generation == 3
