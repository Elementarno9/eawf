"""A completion counts a receipt only as its proof run filed it.

A caller presents the receipts a completion reuses, but the receipt a
completion may rest on is the one the daemon's own proof run filed. A
presented copy whose result, exit status or stamps differ from the filed
line is a receipt the caller wrote, so it completes nothing even when its
freshness key names a leg the daemon did run.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from tests.integration.runtime.daemon._delivery_verb_fixtures import (
    completion_params,
    proof_line,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    TASK_URN,
    document_path,
    method_context,
    provision,
    seed,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _passing() -> tuple[Any, ...]:
    """Return the passing receipts that prove the shared Task at its delivery."""
    contracts = world.compiled("CR-01", "CR-02")
    return world.receipts_at(
        contracts, revision_binding=world.delivering_generation().integrated_revision
    )


def _canary(tmp_path: Path, proofs: tuple[LedgerRecord, ...]) -> CanaryProvision:
    """Provision a canary whose Task is sealed, delivered, and proved by *proofs*."""
    canary = provision(tmp_path / "repo", code="DLV")
    seed(canary, {"task": {"EAWF-0042": world.task_row()}})
    lines = (
        world.bundle_line(world.bundle_row()),
        world.generation_line(world.delivering_generation()),
        *proofs,
    )
    for line in lines:
        append_ledger_record(ledger_path(document_path(canary), line.collection), line)
    return canary


def _complete(canary: CanaryProvision, tmp_path: Path, receipts: tuple[Any, ...]) -> dict[str, Any]:
    """Ask ``domain.task.complete`` to finish the Task on *receipts*."""
    payload: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": TASK_URN,
        "expected_revision": 1,
        "idempotency_key": "complete-EAWF-0042",
        "actor": "OP-0001",
        **completion_params(receipts=[item.model_dump(mode="json") for item in receipts]),
    }
    return asyncio.run(
        methods.dispatch("domain.task.complete", method_context(tmp_path / "runtime"), payload)
    )


def test_r01_completion_refuses_a_filed_fail_restated_as_pass(tmp_path: Path) -> None:
    """The proof run filed FAIL; a PASS copy of that receipt completes nothing."""
    passing = _passing()
    failed = tuple(
        item.model_copy(update={"result": GateReceiptResult.FAIL, "exit_status": 1})
        for item in passing
    )
    canary = _canary(tmp_path, tuple(proof_line(item) for item in failed))
    before = document_path(canary).read_bytes()

    answer = _complete(canary, tmp_path, passing)

    assert answer["errors"][0]["code"] == DomainErrorCode.TRANSITION_GUARD_FAILED.value
    assert "completion_receipt_unfiled" in answer["errors"][0]["message"]
    assert document_path(canary).read_bytes() == before


def test_r01_completion_refuses_a_filed_pass_presented_with_other_stamps(
    tmp_path: Path,
) -> None:
    """A copy differing from the filed line in any field is not the filed receipt."""
    passing = _passing()
    canary = _canary(tmp_path, tuple(proof_line(item) for item in passing))
    restated = (passing[0].model_copy(update={"exit_status": 7}), *passing[1:])

    answer = _complete(canary, tmp_path, restated)

    assert "completion_receipt_unfiled" in answer["errors"][0]["message"]
    assert "G-01" in answer["errors"][0]["message"]


def test_r01_completion_on_the_filed_passing_receipts_commits(tmp_path: Path) -> None:
    """The positive control: the exact filed PASS lines complete the Task."""
    passing = _passing()
    canary = _canary(tmp_path, tuple(proof_line(item) for item in passing))

    answer = _complete(canary, tmp_path, passing)

    assert answer["status"] == "ok", answer["errors"]


def test_r01_a_later_passing_run_of_a_failed_leg_counts(tmp_path: Path) -> None:
    """A leg filed FAIL then proved again PASS completes on the passing line."""
    passing = _passing()
    failed = passing[0].model_copy(
        update={"id": "RCP-0099", "result": GateReceiptResult.FAIL, "exit_status": 1}
    )
    canary = _canary(tmp_path, tuple(proof_line(item) for item in (failed, *passing)))

    answer = _complete(canary, tmp_path, passing)

    assert answer["status"] == "ok", answer["errors"]
