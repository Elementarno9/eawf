"""UI-020, UI-063: each verification-site verdict is scored against what its subject did.

A verdict's subject held when its Batch merged and nothing refuted it; a repair bounded
to its criterion, or a head move that dropped it, refuted it; anything else is unsettled
and scores nothing. A principal's gold label overrides the outcome of the subject's
newest verdict. Each verdict is one juror's ballot, so the validation report scores it
without an agreement statistic that needs two jurors.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

import pytest

from eawf.kernel.delivery.batch_proof import AuditVerdict, BatchVerificationStage
from eawf.kernel.delivery.gold_label import AuditGoldLabel
from eawf.kernel.delivery.integration import ConflictExit, ConflictExitKind
from eawf.kernel.state.enums import AgentSessionRole
from eawf.observability.eval.jury_validation import (
    JuryValidationConfig,
    JuryValidationStatus,
    LabelSource,
    validate_jury,
)
from eawf.observability.eval.native_cohort import (
    ObservedOutcome,
    OutcomeSource,
    native_cohort,
    observe_verdict_outcomes,
)
from tests.unit.kernel.delivery.test_batch_proof import (
    AT,
    BATCH,
    REPAIR_TASK,
    REVIEWER,
    THIRD_HEAD,
    audit,
    binding,
    cycle,
)

PRODUCERS: Final = {REVIEWER.rsplit("/", 1)[-1]: (AgentSessionRole.REVIEWER, "claude-code")}


def _label(criterion_id: str, *, ground_truth: bool) -> AuditGoldLabel:
    return AuditGoldLabel(
        batch_ref=BATCH,
        criterion_id=criterion_id,
        ground_truth=ground_truth,
        labeled_by="OPERATOR",
        labeled_at=AT + timedelta(hours=1),
        note="the criterion did not hold once the release shipped",
    )


def _outcomes(*lines: object, merged: bool) -> dict[str, tuple[object, object]]:
    observed = observe_verdict_outcomes(
        lines,  # type: ignore[arg-type]
        merged_batches=frozenset({"BAT-0007"}) if merged else frozenset(),
    )
    return {item.audit.criterion_id: (item.outcome, item.source) for item in observed}


def test_ui_063_a_verdict_on_a_merged_batch_nothing_refuted_held() -> None:
    held = cycle(audits=(audit("CR-01"),))
    assert _outcomes(held, merged=True) == {"CR-01": (ObservedOutcome.HELD, OutcomeSource.MERGED)}


def test_ui_063_a_verdict_on_a_batch_that_has_not_merged_is_unsettled() -> None:
    assert _outcomes(cycle(audits=(audit("CR-01"),)), merged=False) == {"CR-01": (None, None)}


def test_ui_063_a_repair_bounded_to_the_criterion_refutes_even_after_a_merge() -> None:
    failing = audit("CR-02", verdict=AuditVerdict.VERIFIED_FALSE)
    repaired = cycle(
        stage=BatchVerificationStage.REPAIR,
        audits=(audit("CR-01"), failing),
        repairs_spent=1,
        exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
    )

    outcomes = _outcomes(repaired, merged=True)

    assert outcomes["CR-02"] == (ObservedOutcome.REFUTED, OutcomeSource.REPAIR)
    assert outcomes["CR-01"] == (ObservedOutcome.HELD, OutcomeSource.MERGED)


def test_ui_063_a_head_move_that_drops_the_verdict_refutes_it_and_a_carried_one_holds() -> None:
    first = cycle(audits=(audit("CR-01"), audit("CR-02")))
    moved = cycle(
        stage=BatchVerificationStage.CHECKING,
        head=binding(generation=3, head_sha=THIRD_HEAD),
        audits=(audit("CR-01"),),
    )

    outcomes = _outcomes(first, moved, merged=True)

    assert outcomes["CR-02"] == (ObservedOutcome.REFUTED, OutcomeSource.HEAD_INVALIDATION)
    assert outcomes["CR-01"] == (ObservedOutcome.HELD, OutcomeSource.MERGED)


def test_ui_063_a_verdict_carried_across_passes_is_observed_once() -> None:
    same = cycle(audits=(audit("CR-01"),))
    assert len(observe_verdict_outcomes([same, same], merged_batches=frozenset())) == 1


def test_ui_063_an_empty_ledger_observes_nothing() -> None:
    assert observe_verdict_outcomes([], merged_batches=frozenset()) == ()


def test_ui_063_the_cohort_scores_settled_verdicts_of_known_producers_only() -> None:
    lines = [
        cycle(
            audits=(
                audit("CR-01"),
                audit("CR-02", verdict=AuditVerdict.UNVERIFIED),
            )
        )
    ]
    observed = observe_verdict_outcomes(lines, merged_batches=frozenset({"BAT-0007"}))

    cohort, ballots = native_cohort(observed, producers=PRODUCERS, labels={})
    unknown, _ = native_cohort(observed, producers={}, labels={})

    assert [row.outcome.base_id for row in cohort.silver] == [f"BAT-0007-{audit('CR-01').id}"]
    assert cohort.silver[0].ground_truth is True and cohort.gold == []
    (ballot,) = ballots[cohort.silver[0].outcome.base_id]
    assert (ballot.agent_role, ballot.runtime) == (AgentSessionRole.REVIEWER, "claude-code")
    assert (unknown.silver, unknown.gold) == ([], [])


def test_ui_063_a_gold_label_overrides_the_newest_verdict_and_scores_it_unsettled() -> None:
    lines = [cycle(audits=(audit("CR-01"),))]
    observed = observe_verdict_outcomes(lines, merged_batches=frozenset())

    cohort, _ = native_cohort(
        observed,
        producers=PRODUCERS,
        labels={("BAT-0007", "CR-01"): _label("CR-01", ground_truth=False)},
    )

    assert cohort.silver == []
    (row,) = cohort.gold
    assert (row.ground_truth, row.label_source) == (False, LabelSource.GOLD)
    assert row.outcome.held is None


def test_ui_063_a_gold_label_leaves_an_earlier_refuted_verdict_on_its_outcome() -> None:
    earlier = audit("CR-01", verdict=AuditVerdict.VERIFIED_FALSE)
    later_head = binding(generation=3, head_sha=THIRD_HEAD)
    later = audit("CR-01", audited=later_head, audit_id="BAU-000002")
    lines = [
        cycle(audits=(earlier,)),
        cycle(head=later_head, audits=(later,)),
    ]
    observed = observe_verdict_outcomes(lines, merged_batches=frozenset({"BAT-0007"}))

    cohort, _ = native_cohort(
        observed,
        producers=PRODUCERS,
        labels={("BAT-0007", "CR-01"): _label("CR-01", ground_truth=True)},
    )

    assert [(row.ground_truth, row.outcome.outcome_source) for row in cohort.silver] == [
        (False, "head_invalidation")
    ]
    assert [row.outcome.base_id for row in cohort.gold] == ["BAT-0007-BAU-000002"]


def test_ui_020_a_one_juror_cohort_scores_brier_and_co_error_but_no_agreement() -> None:
    clearing = [audit(f"CR-{i:02d}") for i in range(1, 4)]
    failing = audit("CR-09", verdict=AuditVerdict.VERIFIED_FALSE)
    lines = [
        cycle(
            stage=BatchVerificationStage.REPAIR,
            audits=(*clearing, failing),
            repairs_spent=1,
            exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
        )
    ]
    observed = observe_verdict_outcomes(lines, merged_batches=frozenset({"BAT-0007"}))
    cohort, ballots = native_cohort(observed, producers=PRODUCERS, labels={})

    report = validate_jury(cohort, ballots, JuryValidationConfig(min_validation_n=4))

    assert report.status is JuryValidationStatus.SCORED
    assert (report.n, report.known_bad_n) == (4, 1)
    assert report.brier == pytest.approx(0.0)
    assert report.unanimous_pass_on_known_bad_rate == pytest.approx(0.0)
    assert report.fleiss_kappa is None
