"""Filed proof receipts are numbered in sequence, and a keyed proof retry replays.

A receipt id names one receipt. Deriving it from a hash of the freshness
key folded into four digits made two receipts share an id long before the
key space filled, and made a failing run and a later passing run of one
leg indistinguishable by id. The proof verb numbers every receipt it
files from the repository's own sequence, read under the lock the lines
are appended under, and refuses once that space is full. Its idempotency
key is honoured: a retry under it answers what the first run answered
without running a gate again.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.store.ledger import append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import delivery_proof
from tests.integration.runtime.daemon._delivery_verb_fixtures import proof_line
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import TASK_URN, document_path
from tests.integration.runtime.daemon.test_delivery_landed_loop import (
    _adopt,
    _dispatch,
    gates_file,
    landed_canary,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

PROVE = "runtime.delivery.prove_task"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _adopted(tmp_path: Path) -> Path:
    """Return the document of a landed canary whose change is adopted."""
    canary, base, head = landed_canary(tmp_path)
    _adopt(canary, tmp_path, base_commit=base, head_sha=head)
    return document_path(canary)


def _gates(tmp_path: Path, *, exit_code: int = 0) -> list[dict[str, Any]]:
    """Return the two gates of the landed change, passing or failing."""
    gates = orjson.loads(Path(gates_file(tmp_path, exit_code=exit_code)).read_bytes())["gates"]
    assert isinstance(gates, list)
    return gates


def _filed_ids(path: Path) -> list[str]:
    """Return the id of every filed proof receipt, in filing order."""
    return [
        item.payload["receipt"]["id"]
        for item in read_ledger_records(ledger_path(path, Epoch2Collection.RECEIPT))
        if item.payload.get("payload_kind") == "proof_receipt"
    ]


def _seed_proof(path: Path, receipt_id: str) -> None:
    """File one foreign proof receipt under *receipt_id*, as another run left it."""
    contracts = world.compiled("CR-01", "CR-02")
    receipt = world.receipt(
        contracts.contracts[0],
        revision_binding=world.delivering_generation().integrated_revision,
        receipt_id=receipt_id,
        result=GateReceiptResult.FAIL,
    )
    append_ledger_record(ledger_path(path, Epoch2Collection.RECEIPT), proof_line(receipt))


def test_a_failed_then_passing_run_files_four_distinct_sequential_ids(
    tmp_path: Path,
) -> None:
    """Each receipt gets the next number, so a rerun of a leg is its own receipt."""
    path = _adopted(tmp_path)

    failed = _dispatch(
        PROVE, tmp_path, urn=TASK_URN, idempotency_key="p1", gates=_gates(tmp_path, exit_code=1)
    )
    passed = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p2", gates=_gates(tmp_path))

    assert _filed_ids(path) == ["RCP-0001", "RCP-0002", "RCP-0003", "RCP-0004"]
    assert [leg["receipt_id"] for leg in failed["legs"]] == ["RCP-0001", "RCP-0002"]
    assert [leg["receipt_id"] for leg in passed["legs"]] == ["RCP-0003", "RCP-0004"]


def test_numbering_continues_after_the_highest_filed_id(tmp_path: Path) -> None:
    """A receipt another Task's run filed is counted, so no id is reused."""
    path = _adopted(tmp_path)
    _seed_proof(path, "RCP-0041")

    answer = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=_gates(tmp_path))

    assert [leg["receipt_id"] for leg in answer["legs"]] == ["RCP-0042", "RCP-0043"]


def test_a_full_receipt_key_space_is_refused(tmp_path: Path) -> None:
    """The four-digit space ends at 9999; the next receipt is refused, not wrapped."""
    path = _adopted(tmp_path)
    _seed_proof(path, "RCP-9999")

    with pytest.raises(methods.DaemonValidationError, match="receipt_key_space_saturated"):
        _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=_gates(tmp_path))

    assert _filed_ids(path) == ["RCP-9999"]


def test_proof_retried_under_its_key_replays_without_running_a_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry answers the first run's answer, and no gate runs a second time."""
    path = _adopted(tmp_path)
    gates = _gates(tmp_path, exit_code=1)
    first = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates)

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a keyed retry must not run a gate again")

    monkeypatch.setattr(delivery_proof, "_run_leg", unreachable)
    again = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates)

    assert again == first
    assert len(_filed_ids(path)) == 2


def test_proof_key_naming_other_gates_is_refused(tmp_path: Path) -> None:
    """One key cannot name two different proof runs."""
    _adopted(tmp_path)
    _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=_gates(tmp_path))

    with pytest.raises(methods.DaemonValidationError, match="idempotency_conflict"):
        _dispatch(
            PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=_gates(tmp_path, exit_code=1)
        )


def test_assessment_takes_no_idempotency_key(tmp_path: Path) -> None:
    """The read-only completion verb refuses a key it would never read."""
    _adopted(tmp_path)

    with pytest.raises(methods.DaemonValidationError, match="idempotency_key"):
        _dispatch(
            "runtime.delivery.assess_completion",
            tmp_path,
            urn=TASK_URN,
            idempotency_key="k",
            base=world.BASE.model_dump(mode="json"),
            report_verdict="pass",
            gates=[world.gate("CR-01").model_dump(mode="json")],
            proof_facts=world.facts().model_dump(mode="json"),
        )
