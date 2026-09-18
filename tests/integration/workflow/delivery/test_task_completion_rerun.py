"""DEL-010: rerun what the integrations moved, carry what they did not.

The world under test is one Batch whose base is generation one, a
generation two that delivered the Task, and a generation three that
delivered somebody else while naming exactly one of this Task's criteria
as invalidated. The named criterion is required again at the head the
Batch is delivering; the unnamed one stays proved where it was proved.
That split is what keeps the cost of finishing a Task a function of what
moved rather than of how many Tasks the Batch holds.

Nothing here decides affectedness by reading a path list. A generation's
affected set digests to the criterion component of that generation's own
revision binding, so widening the set after the line was written breaks
the key it was written under, and the walk refuses the line rather than
believing it. The same comparison guards the line itself: a proof bound
to an ordinal at a head the Batch does not record there was taken on a
generation that lost.

Nothing sleeps, polls or reaches outside ``tmp_path``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.delivery.receipts import (
    FreshnessComponent,
    ReuseReason,
    RevisionRefKind,
)
from eawf.kernel.state.enums import AgentReportVerdict, GateReceiptResult
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import TaskCompletionParams, assess_task_completion
from eawf.runtime.integration.apply import IntegrationRefusal
from eawf.workflow.delivery.completion import (
    CompletionRefusal,
    CompletionRefusedError,
    decide_task_completion,
    selected_line,
)
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def decide(
    *,
    generations,
    receipts,
    contracts=None,
    now=None,
    max_age=None,
):
    """Return the completion decision for the shared Task on *generations*."""
    compiled = world.compiled("CR-01", "CR-02") if contracts is None else contracts
    return decide_task_completion(
        world.task(),
        report_verdict=AgentReportVerdict.PASS,
        bundle=world.bundle(),
        ledger=world.ledger(*generations),
        base=world.BASE,
        contracts=compiled,
        receipts=receipts,
        facts=world.facts(),
        now=now,
        max_age=max_age,
    )


def proofs_at_delivery(contracts=None):
    """Return one passing receipt per gate, taken at the delivering generation."""
    compiled = world.compiled("CR-01", "CR-02") if contracts is None else contracts
    return world.receipts_at(
        compiled, revision_binding=world.delivering_generation().integrated_revision
    )


def test_decide_task_completion_reruns_only_the_affected_criterion() -> None:
    """The criterion the following generation named reruns; the other carries."""
    decision = decide(
        generations=(world.delivering_generation(), world.following_generation()),
        receipts=proofs_at_delivery(),
    )
    assert decision.affected_criterion_ids == ("CR-01",)
    assert decision.rerun_gate_ids == ("G-01",)
    assert decision.reused_gate_ids == ("G-02",)
    assert not decision.completable


def test_decide_task_completion_names_the_inputs_that_moved_under_the_rerun() -> None:
    """A rerun says which freshness components the integration changed."""
    decision = decide(
        generations=(world.delivering_generation(), world.following_generation()),
        receipts=proofs_at_delivery(),
    )
    (rerun,) = decision.plan.reruns
    assert rerun.reason is ReuseReason.STALE
    assert FreshnessComponent.CODE in rerun.changed_components
    assert FreshnessComponent.CRITERION in rerun.changed_components


def test_decide_task_completion_accepts_the_rerun_taken_on_the_current_base() -> None:
    """A proof for the affected gate at the head finishes the Task."""
    following = world.following_generation()
    contracts = world.compiled("CR-01", "CR-02")
    carried = world.receipt(
        contracts.contract("G-02"),
        revision_binding=world.delivering_generation().integrated_revision,
        receipt_id="RCP-0002",
    )
    rerun = world.receipt(
        contracts.contract("G-01"),
        revision_binding=following.integrated_revision,
        receipt_id="RCP-0003",
    )
    decision = decide(
        generations=(world.delivering_generation(), following),
        receipts=(carried, rerun),
        contracts=contracts,
    )
    assert decision.rerun_gate_ids == ()
    assert set(decision.reused_gate_ids) == {"G-01", "G-02"}
    assert decision.completable


def test_decide_task_completion_refuses_a_rerun_taken_on_the_older_base() -> None:
    """A second proof of the affected gate at the delivering base is still stale."""
    decision = decide(
        generations=(world.delivering_generation(), world.following_generation()),
        receipts=proofs_at_delivery(),
    )
    assert decision.rerun_gate_ids == ("G-01",)


def test_decide_task_completion_reuses_every_leg_when_nothing_followed() -> None:
    """A Task whose delivery is the head has nothing to rerun."""
    decision = decide(
        generations=(world.delivering_generation(),),
        receipts=proofs_at_delivery(),
    )
    assert decision.affected_criterion_ids == ()
    assert decision.head_generation == decision.delivering_generation == 2
    assert decision.completable


def test_decide_task_completion_walks_every_generation_since_the_delivery() -> None:
    """Two later integrations invalidate the union of what they named."""
    following = world.following_generation(affected=("CR-01",))
    fourth = world.generation(
        ordinal=4,
        head_sha="5e" * 20,
        target_base=following.integrated_revision,
        tasks=(world.OTHER_TASK,),
        affected=("CR-02",),
    )
    decision = decide(
        generations=(world.delivering_generation(), following, fourth),
        receipts=proofs_at_delivery(),
    )
    assert decision.affected_criterion_ids == ("CR-01", "CR-02")
    assert set(decision.rerun_gate_ids) == {"G-01", "G-02"}


def test_decide_task_completion_refuses_a_proof_on_a_superseded_generation() -> None:
    """A receipt bound to ordinal two at a head the Batch never kept is refused."""
    losing = world.binding(generation=2, head_sha=world.LOSING_HEAD, affected=("CR-01", "CR-02"))
    contracts = world.compiled("CR-01", "CR-02")
    with pytest.raises(CompletionRefusedError) as caught:
        decide(
            generations=(world.delivering_generation(), world.following_generation()),
            receipts=world.receipts_at(contracts, revision_binding=losing),
            contracts=contracts,
        )
    assert caught.value.code is CompletionRefusal.GENERATION_SUPERSEDED
    assert "selected line records at that ordinal" in str(caught.value)


def test_decide_task_completion_refuses_a_proof_bound_beyond_the_head() -> None:
    """An ordinal the Batch has not reached is not a generation it records."""
    ahead = world.binding(generation=9, head_sha="6f" * 20)
    contracts = world.compiled("CR-01", "CR-02")
    with pytest.raises(CompletionRefusedError) as caught:
        decide(
            generations=(world.delivering_generation(), world.following_generation()),
            receipts=world.receipts_at(contracts, revision_binding=ahead),
            contracts=contracts,
        )
    assert caught.value.code is CompletionRefusal.GENERATION_SUPERSEDED


def test_decide_task_completion_ignores_a_proof_taken_on_a_candidate_ref() -> None:
    """A workspace proof is not a Batch proof, so it reruns instead of refusing."""
    contracts = world.compiled("CR-01", "CR-02")
    candidate = world.binding(
        generation=1, head_sha=world.BASE_COMMIT, ref_kind=RevisionRefKind.CANDIDATE
    )
    decision = decide(
        generations=(world.delivering_generation(),),
        receipts=world.receipts_at(contracts, revision_binding=candidate),
        contracts=contracts,
    )
    assert set(decision.rerun_gate_ids) == {"G-01", "G-02"}


def test_decide_task_completion_refuses_an_affected_set_unbound_from_its_key() -> None:
    """A line whose affected set was widened after the fact is not believed."""
    honest = world.following_generation(affected=("CR-01",))
    widened = honest.model_copy(update={"affected_criterion_ids": ("CR-01", "CR-02")})
    with pytest.raises(CompletionRefusedError) as caught:
        decide(
            generations=(world.delivering_generation(), widened),
            receipts=proofs_at_delivery(),
        )
    assert caught.value.code is CompletionRefusal.AFFECTED_SET_UNBOUND


def test_decide_task_completion_reruns_a_receipt_that_did_not_pass() -> None:
    """A carried leg still needs a passing proof, not merely a matching key."""
    contracts = world.compiled("CR-01", "CR-02")
    binding = world.delivering_generation().integrated_revision
    failed = world.receipt(
        contracts.contract("G-02"),
        revision_binding=binding,
        receipt_id="RCP-0009",
        result=GateReceiptResult.FAIL,
    )
    decision = decide(
        generations=(world.delivering_generation(),),
        receipts=(
            world.receipt(contracts.contract("G-01"), revision_binding=binding),
            failed,
        ),
        contracts=contracts,
    )
    assert decision.rerun_gate_ids == ("G-02",)
    assert decision.plan.reruns[0].reason is ReuseReason.NOT_PASSED


def test_decide_task_completion_reruns_a_receipt_past_the_age_limit() -> None:
    """An age limit retires a carried proof even where nothing moved."""
    decision = decide(
        generations=(world.delivering_generation(),),
        receipts=proofs_at_delivery(),
        now=NOW,
        max_age=timedelta(minutes=1),
    )
    assert set(decision.rerun_gate_ids) == {"G-01", "G-02"}
    assert {item.reason for item in decision.plan.reruns} == {ReuseReason.EXPIRED}


def test_decide_task_completion_reruns_a_leg_with_no_receipt_at_all() -> None:
    """The empty receipt pile is the boundary case: every leg is missing."""
    decision = decide(generations=(world.delivering_generation(),), receipts=())
    assert set(decision.rerun_gate_ids) == {"G-01", "G-02"}
    assert {item.reason for item in decision.plan.reruns} == {ReuseReason.MISSING}


def test_decide_task_completion_rejects_a_lone_age_argument() -> None:
    """An age limit without a decision time cannot be applied."""
    with pytest.raises(ValueError, match="now and max_age must be given together"):
        decide(
            generations=(world.delivering_generation(),),
            receipts=proofs_at_delivery(),
            max_age=timedelta(days=1),
        )


def test_decide_task_completion_rejects_a_ledger_of_another_batch() -> None:
    """A history of another Batch answers nothing about this Task."""
    with pytest.raises(ValueError, match="not the Task's"):
        decide_task_completion(
            world.task(batch_ref=world.OTHER_BATCH),
            report_verdict=AgentReportVerdict.PASS,
            bundle=world.bundle(),
            ledger=world.ledger(world.delivering_generation()),
            base=world.BASE,
            contracts=world.compiled("CR-01", "CR-02"),
            receipts=(),
            facts=world.facts(),
        )


def test_completion_refusal_reuses_the_integration_superseded_code() -> None:
    """The superseded code is the integration vocabulary, not a second spelling."""
    assert CompletionRefusal.GENERATION_SUPERSEDED == IntegrationRefusal.GENERATION_SUPERSEDED
    assert CompletionRefusal.GENERATION_SUPERSEDED.value == "integration_generation_superseded"


def test_selected_line_records_the_base_and_every_generation() -> None:
    """The line the supersession check reads carries the base at ordinal one."""
    line = selected_line(
        world.ledger(world.delivering_generation(), world.following_generation()),
        base=world.BASE,
    )
    assert sorted(line) == [1, 2, 3]
    assert line[1].head_sha == world.BASE_COMMIT
    assert line[3].head_sha == world.THIRD_HEAD


def native(tmp_path: Path, *generations):
    """Return a canary tree seeded with the Task, its seal and *generations*."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    world.seed_lines(context, world.BATCH, [world.generation_line(item) for item in generations])
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


def test_assess_task_completion_reruns_the_affected_gate_through_the_verb(
    tmp_path: Path,
) -> None:
    """The daemon verb reads the tree and answers the same split."""
    context = native(tmp_path, world.delivering_generation(), world.following_generation())
    answer = assess_task_completion(context, params(receipts=proofs_at_delivery()))
    assert answer.affected_criterion_ids == ("CR-01",)
    assert answer.rerun_gate_ids == ("G-01",)
    assert answer.reused_gate_ids == ("G-02",)
    assert answer.head_generation == 3
    assert answer.delivering_generation == 2
    assert not answer.completable


def test_assess_task_completion_refuses_a_superseded_proof_through_the_verb(
    tmp_path: Path,
) -> None:
    """A proof anchored off the Batch line is refused on the wire too."""
    context = native(tmp_path, world.delivering_generation(), world.following_generation())
    losing = world.binding(generation=2, head_sha=world.LOSING_HEAD, affected=("CR-01", "CR-02"))
    contracts = world.compiled("CR-01", "CR-02")
    with pytest.raises(DaemonValidationError, match="integration_generation_superseded"):
        assess_task_completion(
            context, params(receipts=world.receipts_at(contracts, revision_binding=losing))
        )
