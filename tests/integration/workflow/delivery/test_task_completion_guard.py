"""DEL-001: a reported success is not a delivery, and cannot finish a Task.

An executor reporting ``pass`` has said something about a workspace. Two
durable facts turn that into a finished Task, and this suite drives the
absence of each: the seal, which is the daemon's own record that a tree
was accepted for integration, and a selected generation whose work names
this Task, which is the Batch saying the head it is delivering carries it.
Either one missing leaves the Task where it was.

The positive control runs beside them. A world with both facts and a
matching proof completes, so a refusal here is the guard firing rather
than the fixture never having been completable in the first place.

Nothing sleeps, polls or reaches outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.delivery.receipts import ReuseDisposition
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import TaskCompletionParams, assess_task_completion
from eawf.workflow.delivery.completion import (
    CompletionRefusal,
    CompletionRefusedError,
    decide_task_completion,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

#: Distinguishes "leave this fact at its default" from "this fact is absent",
#: which is the very difference the guard is about.
DEFAULT: Final = object()


def delivered_ledger():
    """Return a Batch whose one generation carries the Task under test."""
    return world.ledger(world.delivering_generation())


def decide(
    *,
    task=None,
    report_verdict: AgentReportVerdict = AgentReportVerdict.PASS,
    bundle: Any = DEFAULT,
    ledger=None,
    contracts=None,
    receipts=(),
):
    """Return the completion decision for one arrangement of the two facts."""
    return decide_task_completion(
        world.task() if task is None else task,
        report_verdict=report_verdict,
        bundle=world.bundle() if bundle is DEFAULT else bundle,
        ledger=delivered_ledger() if ledger is None else ledger,
        base=world.BASE,
        contracts=world.compiled("CR-01", "CR-02") if contracts is None else contracts,
        receipts=receipts,
        facts=world.facts(),
    )


def test_decide_task_completion_completes_when_both_facts_hold() -> None:
    """The positive control: a seal, a generation carrying it and fresh proof."""
    contracts = world.compiled("CR-01", "CR-02")
    decision = decide(
        contracts=contracts,
        receipts=world.receipts_at(
            contracts, revision_binding=world.delivering_generation().integrated_revision
        ),
    )
    assert decision.completable
    assert decision.rerun_gate_ids == ()


def test_decide_task_completion_refuses_a_reported_success_with_no_seal() -> None:
    """A pass nobody sealed is a claim about a workspace, not a delivery."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(bundle=None)
    assert caught.value.code is CompletionRefusal.BUNDLE_UNSEALED
    assert "claim about a workspace" in str(caught.value)


def test_decide_task_completion_refuses_a_seal_taken_for_another_task() -> None:
    """Somebody else's sealed tree does not deliver this Task."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(bundle=world.bundle(task_ref=world.OTHER_TASK))
    assert caught.value.code is CompletionRefusal.BUNDLE_UNSEALED


def test_decide_task_completion_refuses_when_no_generation_is_selected() -> None:
    """A Batch that integrated nothing has no head to have completed on."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(ledger=world.ledger())
    assert caught.value.code is CompletionRefusal.GENERATION_UNSELECTED


def test_decide_task_completion_refuses_when_no_generation_carries_the_task() -> None:
    """A head that delivered somebody else did not deliver this Task."""
    elsewhere = world.generation(
        ordinal=2,
        head_sha=world.SECOND_HEAD,
        target_base=world.BASE,
        tasks=(world.OTHER_TASK,),
    )
    with pytest.raises(CompletionRefusedError) as caught:
        decide(ledger=world.ledger(elsewhere))
    assert caught.value.code is CompletionRefusal.TASK_UNDELIVERED


@pytest.mark.parametrize("verdict", [AgentReportVerdict.FAIL, AgentReportVerdict.BLOCKED])
def test_decide_task_completion_refuses_an_unsuccessful_report(
    verdict: AgentReportVerdict,
) -> None:
    """A report that does not propose its work for delivery completes nothing."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(report_verdict=verdict)
    assert caught.value.code is CompletionRefusal.REPORT_UNSUCCESSFUL


def test_decide_task_completion_accepts_a_followed_up_pass() -> None:
    """The boundary of the deliverable verdicts is inclusive of followups."""
    decision = decide(report_verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    assert decision.delivering_generation == 2


def test_decide_task_completion_refuses_a_promise_covered_by_no_gate() -> None:
    """A criterion nothing compiles a contract for would pass unproved."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(
            task=world.task(attested=("CR-02",)),
            contracts=world.compiled("CR-01", "CR-02", attested=("CR-02",)),
        )
    assert caught.value.code is CompletionRefusal.CRITERIA_UNCOVERED
    assert "CR-02" in str(caught.value)


def test_decide_task_completion_refuses_contracts_of_other_criteria() -> None:
    """Contracts compiled for a narrower set would decide over the wrong rows."""
    with pytest.raises(CompletionRefusedError) as caught:
        decide(contracts=world.compiled("CR-01"))
    assert caught.value.code is CompletionRefusal.CRITERIA_UNCOVERED


def test_decide_task_completion_rejects_an_unplaced_task() -> None:
    """A Task in the backlog sits in no Batch, so no head can carry it."""
    with pytest.raises(ValueError, match="unplaced"):
        decide(task=world.task(batch_ref=None, status=TaskStatus.DRAFT))


def test_decide_task_completion_leaves_a_jury_leg_unsettled() -> None:
    """A criterion a jury settles is unavailable to the deterministic path."""
    contracts = world.compiled("CR-01", "CR-02", jury=("CR-02",))
    decision = decide(
        task=world.task(jury=("CR-02",)),
        contracts=contracts,
        receipts=world.receipts_at(
            contracts, revision_binding=world.delivering_generation().integrated_revision
        ),
    )
    assert decision.unavailable_gate_ids == ("G-02",)
    assert not decision.completable


def test_decide_task_completion_decides_every_leg_exactly_once() -> None:
    """Each compiled gate is settled once, so nothing is silently skipped."""
    decision = decide()
    dispositions = [item.disposition for item in decision.plan.decisions]
    assert len(dispositions) == 2
    assert set(dispositions) == {ReuseDisposition.RERUN}


def native(tmp_path: Path, *, seal: bool = True, generations=()):
    """Return a canary tree seeded with the Task and the facts named."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    if seal:
        world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    if generations:
        world.seed_lines(
            context, world.BATCH, [world.generation_line(item) for item in generations]
        )
    return context


def params(**overrides) -> TaskCompletionParams:
    """Return a completion request for the shared Task."""
    fields = {
        "urn": world.TASK,
        "actor": "OPERATOR-LOCAL",
        "idempotency_key": "completion-1",
        "base": world.BASE,
        "report_verdict": AgentReportVerdict.PASS,
        "gates": (world.gate("CR-01"), world.gate("CR-02")),
        "receipts": (),
        "proof_facts": world.facts(),
    }
    return TaskCompletionParams.model_validate(fields | overrides)


def test_assess_task_completion_refuses_an_unsealed_report(tmp_path: Path) -> None:
    """The verb reads the run ledger and finds no seal to stand the pass on."""
    context = native(tmp_path, seal=False, generations=(world.delivering_generation(),))
    with pytest.raises(DaemonValidationError, match="completion_bundle_unsealed"):
        assess_task_completion(context, params())


def test_assess_task_completion_refuses_an_unintegrated_task(tmp_path: Path) -> None:
    """A sealed tree with no generation behind it still completes nothing."""
    context = native(tmp_path, seal=True)
    with pytest.raises(DaemonValidationError, match="completion_generation_unselected"):
        assess_task_completion(context, params())


def test_assess_task_completion_answers_for_a_delivered_task(tmp_path: Path) -> None:
    """With both facts seeded the verb answers rather than refusing."""
    context = native(tmp_path, seal=True, generations=(world.delivering_generation(),))
    contracts = world.compiled("CR-01", "CR-02")
    answer = assess_task_completion(
        context,
        params(
            receipts=world.receipts_at(
                contracts,
                revision_binding=world.delivering_generation().integrated_revision,
            )
        ),
    )
    assert answer.completable
    assert answer.task_ref == world.TASK
    assert answer.head_generation == 2


def test_assess_task_completion_refuses_a_task_that_is_not_in_the_tree(
    tmp_path: Path,
) -> None:
    """A URN nothing was seeded under names no Task to judge."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    with pytest.raises(DaemonValidationError, match="task_absent"):
        assess_task_completion(context, params())


def test_assess_task_completion_refuses_gates_that_do_not_compile(
    tmp_path: Path,
) -> None:
    """Gates that do not cover the Task's criteria prove nothing about them."""
    context = native(tmp_path, seal=True, generations=(world.delivering_generation(),))
    with pytest.raises(DaemonValidationError, match="completion_criteria_uncovered"):
        assess_task_completion(context, params(gates=(world.gate("CR-01"),)))
