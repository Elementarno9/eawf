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

import re
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


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "pytest", "-q", "--basetemp={temp}/eawf-gate-basetemp"],
        ["git", "grep", "-q", "x = 2", "--", "{temp}/module.py"],
        ["git", "grep", "-q", "x = 2", "-O{temp}/pager", "--", "src/module.py"],
        ["git", "grep", "-q", "-e", "key={temp}/x", "--", "src/module.py"],
    ],
    ids=["option-value", "positional", "short-option", "key-value"],
)
def test_a_gate_naming_an_absolute_temp_path_is_refused_before_any_receipt(
    tmp_path: Path, argv: list[str]
) -> None:
    """A receipt copies its gate's argv into the committed ledger, so a machine path never runs."""
    path = _adopted(tmp_path)
    temp = str(Path(tempfile.gettempdir()).resolve())
    gates = [
        {**gate, "args": {"argv": [token.format(temp=temp) for token in argv]}}
        for gate in _gates(tmp_path)
    ]

    with pytest.raises(methods.DaemonValidationError, match="gate_argv_machine_path"):
        _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates)

    assert _filed_ids(path) == []
    ledger = ledger_path(path, Epoch2Collection.RECEIPT)
    assert not ledger.is_file() or temp not in ledger.read_text(encoding="utf-8")


def test_a_gate_with_a_relative_basetemp_files_its_receipt(tmp_path: Path) -> None:
    """Boundary: a repository-relative path is portable and runs as given."""
    path = _adopted(tmp_path)
    gates = [
        {**gate, "args": {"argv": [*gate["args"]["argv"][:-1], "./src/module.py"]}}
        for gate in _gates(tmp_path)
    ]

    answer = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=gates)

    assert answer["passed"] is True
    assert len(_filed_ids(path)) == 2


def _with_argv(tmp_path: Path, argv: list[str]) -> list[dict[str, Any]]:
    """Return the landed change's gates, each running *argv*."""
    return [{**gate, "args": {"argv": argv}} for gate in _gates(tmp_path)]


def test_a_system_program_and_url_like_paths_are_portable(tmp_path: Path) -> None:
    """Boundary: a path rooted outside a home or temp directory names nothing of this machine."""
    path = _adopted(tmp_path)
    argv = [
        *("git", "grep", "-q", "-e", "x = 2", "-e", "/api/v1/", "-e", "pager=/usr/bin/less"),
        *("--", "src/module.py"),
    ]

    answer = _dispatch(
        PROVE, tmp_path, urn=TASK_URN, idempotency_key="p", gates=_with_argv(tmp_path, argv)
    )

    assert answer["passed"] is True
    assert len(_filed_ids(path)) == 2


def test_a_reused_leg_is_not_refused_for_the_gate_it_already_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a leg that will run is checked: a passing receipt's gate runs nothing again."""
    path = _adopted(tmp_path)
    temp = str(Path(tempfile.gettempdir()).resolve())
    argv = ["git", "grep", "-q", "-e", "x = 2", "-e", f"{temp}/x", "--", "src/module.py"]
    gates = _with_argv(tmp_path, argv)
    with monkeypatch.context() as patched:
        patched.setattr(delivery_proof, "_MACHINE_PATH", re.compile(r"(?!)"))
        first = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p1", gates=gates)
    assert first["passed"] is True

    again = _dispatch(PROVE, tmp_path, urn=TASK_URN, idempotency_key="p2", gates=gates)

    assert again["passed"] is True
    assert {leg["result"] for leg in again["legs"]} == {"reused"}
    assert len(_filed_ids(path)) == 2
