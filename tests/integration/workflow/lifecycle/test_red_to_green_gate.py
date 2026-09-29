"""LINT-033: a required-tier Task completes only on a failing-then-passing proof pair.

A gate that runs a defect repro test makes its Task required-tier. The
completion judgment reads the proofs the daemon filed for the Task, oldest
first, and refuses to bind a completion until that gate holds a failing
proof before its passing one: a fix whose repro never failed has shown
nothing about the defect. The world is the shared delivery fixture, one
Batch whose generation two carried the Task, with the proofs seeded in the
receipt ledger exactly as ``runtime.delivery.prove_task`` files them.

Nothing sleeps, polls or reaches outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import AgentReportVerdict, GateReceiptResult
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods.delivery_completion import (
    PROOF_KEY_PREFIX,
    FiledProof,
    TaskCompletionParams,
    completion_binding,
)
from eawf.workflow.delivery.criteria import (
    CriteriaAuthoring,
    ExecutionContractSet,
    compile_execution_contracts,
)
from eawf.workflow.verify.red_to_green import (
    GateRun,
    is_required_tier,
    unpaired_required_gates,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

REPRO = "tests/unit/sample/test_publish.py::test_publish_repro_stale_wheel"


def repro_gate() -> GateSpec:
    """Return the CR-01 gate, running a defect repro test."""
    return world.gate("CR-01").model_copy(update={"args": {"argv": ["uv", "run", "pytest", REPRO]}})


def gates() -> tuple[GateSpec, ...]:
    """Return the Task's gates: the repro gate and an ordinary one."""
    return (repro_gate(), world.gate("CR-02"))


def contracts() -> ExecutionContractSet:
    """Return the Task's criteria compiled against :func:`gates`."""
    return compile_execution_contracts(
        CriteriaAuthoring(
            scope_id="EAWF-0042", criteria=world.criteria("CR-01", "CR-02"), gates=gates()
        )
    )


def proof(gate_id: str, result: GateReceiptResult, receipt_id: str) -> ProofReceipt:
    """Return one proof of *gate_id* at the delivering generation."""
    return world.receipt(
        contracts().contract(gate_id),
        revision_binding=world.delivering_generation().integrated_revision,
        receipt_id=receipt_id,
        result=result,
    )


def file_proofs(context: Epoch2RootContext, receipts: tuple[ProofReceipt, ...]) -> None:
    """Append *receipts* to the receipt ledger as the daemon's proof run files them."""
    by_id = {gate.id: gate for gate in gates()}
    lines = [
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key=f"{PROOF_KEY_PREFIX}{item.id}-EAWF-0042",
            status=item.result.value,
            recorded_at=item.ended_at,
            payload=FiledProof.model_validate(
                {"task_ref": world.TASK, "gate": by_id[item.gate_id], "receipt": item}
            ).model_dump(mode="json"),
        )
        for item in receipts
    ]
    world.seed_lines(context, world.TASK, lines)


def delivered(tmp_path: Path, filed: tuple[ProofReceipt, ...]) -> Epoch2RootContext:
    """Return a tree where the Task is sealed, delivered and *filed* was proved."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    world.seed_lines(context, world.BATCH, [world.generation_line(world.delivering_generation())])
    file_proofs(context, filed)
    return context


def complete(context: Epoch2RootContext, presented: tuple[ProofReceipt, ...]):
    """Ask the completion move for the binding it would record."""
    params = TaskCompletionParams.model_validate(
        {
            "urn": world.TASK,
            "actor": "OPERATOR-LOCAL",
            "base": world.BASE,
            "report_verdict": AgentReportVerdict.PASS,
            "gates": gates(),
            "receipts": presented,
            "proof_facts": world.facts(),
        }
    )
    head = world.delivering_generation().integrated_revision.head_sha
    return completion_binding(context, params, integrated_commit=head)


def test_lint_033_required_tier_task_without_a_red_proof_cannot_complete(tmp_path: Path) -> None:
    """Gate fire: both legs proved green, but the repro never ran red."""
    green = (
        proof("G-01", GateReceiptResult.PASS, "RCP-0002"),
        proof("G-02", GateReceiptResult.PASS, "RCP-0003"),
    )
    answer, binding = complete(delivered(tmp_path, green), green)

    assert binding is None
    assert not answer.completable
    assert "G-01" in answer.reason
    assert "no earlier red run" in answer.reason


def test_lint_033_required_tier_task_completes_on_a_red_then_green_pair(tmp_path: Path) -> None:
    """The positive control: the repro failed first, then passed after the fix."""
    red = proof("G-01", GateReceiptResult.FAIL, "RCP-0001")
    green = (
        proof("G-01", GateReceiptResult.PASS, "RCP-0002"),
        proof("G-02", GateReceiptResult.PASS, "RCP-0003"),
    )
    answer, binding = complete(delivered(tmp_path, (red, *green)), green)

    assert answer.completable, answer.reason
    assert binding is not None


def test_lint_033_a_green_before_the_red_does_not_make_a_pair(tmp_path: Path) -> None:
    """A red proof filed after the first green is not the red the fix turned."""
    green = (
        proof("G-01", GateReceiptResult.PASS, "RCP-0002"),
        proof("G-02", GateReceiptResult.PASS, "RCP-0003"),
    )
    red_after = proof("G-01", GateReceiptResult.FAIL, "RCP-0004")
    later_green = proof("G-01", GateReceiptResult.PASS, "RCP-0005")
    answer, binding = complete(
        delivered(tmp_path, (*green, red_after, later_green)), (later_green, green[1])
    )

    assert binding is None
    assert "no earlier red run" in answer.reason


def test_lint_033_an_optional_tier_task_needs_no_red_proof() -> None:
    """A gate that runs no repro test leaves the Task on the optional tier."""
    runs = (GateRun(gate_id="G-02", result=GateReceiptResult.PASS),)

    assert not is_required_tier(world.gate("CR-02"))
    assert unpaired_required_gates((world.gate("CR-02"),), runs) == ()


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        ((), "no run of the named test was recorded"),
        ((GateReceiptResult.FAIL,), "the test never ran green"),
        ((GateReceiptResult.FAIL, GateReceiptResult.PASS, GateReceiptResult.FAIL), "is red"),
        ((GateReceiptResult.ERROR, GateReceiptResult.PASS), "no earlier red run"),
    ],
)
def test_lint_033_unpaired_required_gates_names_the_missing_half(
    results: tuple[GateReceiptResult, ...], reason: str
) -> None:
    """Boundaries: no run, never green, ended red, and an error that is no red."""
    runs = tuple(GateRun(gate_id="G-01", result=result) for result in results)

    (finding,) = unpaired_required_gates((repro_gate(),), runs)

    assert finding.test_id == "G-01"
    assert reason in finding.reason


def test_lint_033_red_then_green_pair_clears_the_required_gate() -> None:
    runs = (
        GateRun(gate_id="G-01", result=GateReceiptResult.FAIL),
        GateRun(gate_id="G-02", result=GateReceiptResult.PASS),
        GateRun(gate_id="G-01", result=GateReceiptResult.PASS),
    )

    assert unpaired_required_gates(gates(), runs) == ()


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "pytest", REPRO],
        ["pytest", "-k", "test_publish_repro_stale_wheel"],
        ["pytest", f"{REPRO}[case-1]"],
    ],
)
def test_lint_033_is_required_tier_reads_the_repro_name_from_argv(argv: list[str]) -> None:
    assert is_required_tier(world.gate("CR-01").model_copy(update={"args": {"argv": argv}}))


@pytest.mark.parametrize(
    "args",
    [{}, {"argv": []}, {"argv": ["pytest", "tests/unit/x.py::test_nonrepro_case"]}],
)
def test_lint_033_is_required_tier_is_false_without_a_repro(args: dict[str, object]) -> None:
    assert not is_required_tier(world.gate("CR-01").model_copy(update={"args": args}))
