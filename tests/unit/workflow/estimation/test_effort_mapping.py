"""The effort-unit mapping policy record, estimate availability and the re-fit."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import ActualStatus, Confidence
from eawf.kernel.state.epoch2.pending_action import OptionEffect
from eawf.kernel.state.models import ActualSummary, EstimateSummary
from eawf.kernel.store.kinds.estimate import EstimatePayload
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.estimation.calibration import (
    NotDueReason,
    RefitDisposition,
    refit_mapping,
)
from eawf.workflow.estimation.mapping import (
    CURRENT_EFFORT_MAPPING,
    EffortDispersion,
    EffortMapping,
    ExclusionCount,
    ExclusionReason,
    MappingFit,
    MappingStatus,
    UnavailableReason,
    estimate_unavailable_reason,
)
from eawf.workflow.estimation.mapping_revisions import (
    AppliedMapping,
    mapping_in_force,
    proposal_key,
    revision_record_key,
)

_T0 = datetime(2026, 9, 1, tzinfo=UTC)
#: A day on which the shipped mapping's cadence has elapsed.
_DUE = CURRENT_EFFORT_MAPPING.effective_on + timedelta(
    days=CURRENT_EFFORT_MAPPING.refit_cadence_days
)


def _estimate(**update: object) -> EstimateSummary:
    base = EstimateSummary(
        id="EST-1",
        scope_id="P01-I01-W01",
        expected_eu=0.8,
        pessimistic_eu=4.1,
        expected_minutes=24.0,
        pessimistic_minutes=123.1,
        display="0.8 EU",
        reference_class="wave",
        reference_sample_size=CURRENT_EFFORT_MAPPING.minimum_reference_sample,
        mapping_revision=CURRENT_EFFORT_MAPPING.revision,
        confidence=Confidence.MEDIUM,
        current_store_record_id="REC-1",
        updated_at=_T0,
    )
    return base.model_copy(update=update)


def _actual(
    subject: str,
    elapsed_eu: float,
    *,
    status: ActualStatus = ActualStatus.DONE,
    excluded: bool = False,
) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{subject}",
        scope_id=subject,
        status=status,
        elapsed_eu=elapsed_eu,
        calibration_excluded=excluded,
        current_store_record_id=f"REC-{subject}",
        updated_at=_T0,
    )


def _actuals(count: int, elapsed_eu: float) -> dict[str, ActualSummary]:
    return {f"W{index:03d}": _actual(f"W{index:03d}", elapsed_eu) for index in range(count)}


# --- MEAS-019 / MEAS-021 / MEAS-045: a typed, versioned policy record ------------------


def test_meas_019_the_mapping_is_a_record_with_a_revision_and_a_digest() -> None:
    mapping = CURRENT_EFFORT_MAPPING
    assert mapping.revision == 2
    assert mapping.digest.startswith("sha256:")
    assert len(mapping.digest) == len("sha256:") + 64
    assert mapping.digest == EffortMapping.model_validate(mapping.model_dump()).digest


@pytest.mark.parametrize(
    "update",
    [{"effort_eu": 0.81}, {"eu_minutes": 31.0}, {"revision": 3}, {"refit_threshold": 0.3}],
)
def test_meas_019_any_change_moves_the_digest(update: dict[str, object]) -> None:
    changed = CURRENT_EFFORT_MAPPING.model_copy(update=update)
    assert changed.digest != CURRENT_EFFORT_MAPPING.digest


def test_meas_019_the_record_is_frozen_and_closed() -> None:
    with pytest.raises(ValidationError):
        CURRENT_EFFORT_MAPPING.effort_eu = 1.0  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        EffortMapping.model_validate({**CURRENT_EFFORT_MAPPING.model_dump(), "ladder": {}})


@pytest.mark.parametrize(
    ("field", "value"),
    [("revision", 0), ("effort_eu", 0.0), ("eu_minutes", -1.0), ("minimum_reference_sample", 0)],
)
def test_meas_019_the_record_refuses_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        EffortMapping.model_validate({**CURRENT_EFFORT_MAPPING.model_dump(), field: value})


def test_meas_019_dispersion_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="out of order"):
        EffortDispersion(p10=30.0, p50=20.0, p90=100.0)


def test_meas_021_the_shipped_mapping_is_a_proposal_default_pending_refit() -> None:
    assert CURRENT_EFFORT_MAPPING.status is MappingStatus.PROPOSAL_DEFAULT
    assert CURRENT_EFFORT_MAPPING.fit is None
    assert CURRENT_EFFORT_MAPPING.effort_minutes == pytest.approx(24.0)


def test_meas_019_a_status_and_its_fit_travel_together() -> None:
    with pytest.raises(ValidationError, match="fitted mapping revision"):
        CURRENT_EFFORT_MAPPING.model_validate(
            {**CURRENT_EFFORT_MAPPING.model_dump(), "status": MappingStatus.FITTED}
        )
    fit = MappingFit(fitted_on=_DUE, input_sample=("W1",), residuals_eu=(0.0,))
    with pytest.raises(ValidationError, match="proposal_default mapping revision"):
        EffortMapping.model_validate({**CURRENT_EFFORT_MAPPING.model_dump(), "fit": fit})


def test_meas_020_a_fit_carries_one_residual_per_sampled_row() -> None:
    with pytest.raises(ValidationError, match="2 residuals for a sample of 1"):
        MappingFit(fitted_on=_DUE, input_sample=("W1",), residuals_eu=(0.1, 0.2))


# --- MEAS-017: the pessimistic value is a declared bound -------------------------------


def test_meas_017_pessimistic_is_the_declared_p90_not_a_multiple() -> None:
    mapping = CURRENT_EFFORT_MAPPING
    assert mapping.pessimistic_minutes == pytest.approx(123.1)
    assert mapping.pessimistic_eu == pytest.approx(123.1 / 30.0)
    doubled = mapping.model_copy(update={"effort_eu": mapping.effort_eu * 2})
    assert doubled.pessimistic_minutes == mapping.pessimistic_minutes


# --- MEAS-013 / MEAS-014 / MEAS-015: estimate fields and availability ------------------


def test_meas_015_legacy_estimates_read_back_with_no_revision_or_sample() -> None:
    legacy = _estimate().model_dump(exclude={"mapping_revision", "reference_sample_size"})
    row = EstimateSummary.model_validate(legacy)
    assert row.mapping_revision is None
    assert row.reference_sample_size is None


@pytest.mark.parametrize(
    ("field", "value"), [("mapping_revision", 0), ("reference_sample_size", -1)]
)
def test_meas_015_estimate_fields_refuse_out_of_range(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        EstimateSummary.model_validate({**_estimate().model_dump(), field: value})
    with pytest.raises(ValidationError, match=field):
        EstimatePayload.model_validate(
            {
                "scope_type": "wave",
                "source": "planner",
                "grain": "wave",
                "expected_eu": 0.8,
                "pessimistic_eu": 4.1,
                "expected_minutes": 24.0,
                "pessimistic_minutes": 123.1,
                "display": "0.8 EU",
                "display_category": "effort",
                "confidence": "medium",
                "coefficients_profile": "default",
                field: value,
            }
        )


def test_meas_013_a_complete_estimate_is_available() -> None:
    assert estimate_unavailable_reason(_estimate(), CURRENT_EFFORT_MAPPING) is None


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"reference_class": None}, UnavailableReason.NO_REFERENCE_CLASS),
        ({"reference_class": ""}, UnavailableReason.NO_REFERENCE_CLASS),
        ({"reference_sample_size": None}, UnavailableReason.NO_SAMPLE_SIZE),
        ({"mapping_revision": None}, UnavailableReason.NO_MAPPING_REVISION),
        ({"reference_sample_size": 0}, UnavailableReason.SAMPLE_BELOW_MINIMUM),
        (
            {"reference_sample_size": CURRENT_EFFORT_MAPPING.minimum_reference_sample - 1},
            UnavailableReason.SAMPLE_BELOW_MINIMUM,
        ),
    ],
)
def test_meas_013_014_an_incomplete_or_thin_estimate_is_unavailable(
    update: dict[str, object], reason: UnavailableReason
) -> None:
    assert estimate_unavailable_reason(_estimate(**update), CURRENT_EFFORT_MAPPING) is reason


# --- MEAS-020 / MEAS-022 / MEAS-023 / MEAS-016: the re-fit ------------------------------


def test_meas_020_no_actuals_is_not_due() -> None:
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, {}, today=_DUE)
    assert outcome.disposition is RefitDisposition.NOT_DUE
    assert outcome.not_due == (NotDueReason.SAMPLE_BELOW_MINIMUM,)
    assert outcome.eligible_count == 0
    assert outcome.proposed is None


def test_meas_020_one_short_of_the_minimum_sample_is_not_due() -> None:
    minimum = CURRENT_EFFORT_MAPPING.refit_minimum_sample
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, _actuals(minimum - 1, 0.8), today=_DUE)
    assert outcome.not_due == (NotDueReason.SAMPLE_BELOW_MINIMUM,)


def test_meas_020_the_cadence_must_elapse() -> None:
    minimum = CURRENT_EFFORT_MAPPING.refit_minimum_sample
    early = _DUE - timedelta(days=1)
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, _actuals(minimum, 0.8), today=early)
    assert outcome.not_due == (NotDueReason.CADENCE_NOT_ELAPSED,)


def test_meas_020_022_a_small_move_applies_as_the_next_revision_with_its_fit() -> None:
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 0.9)
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)

    assert outcome.disposition is RefitDisposition.APPLY
    assert outcome.relative_change == pytest.approx(0.125)
    proposed = outcome.proposed
    assert proposed is not None
    assert proposed.revision == CURRENT_EFFORT_MAPPING.revision + 1
    assert proposed.status is MappingStatus.FITTED
    assert proposed.effort_eu == pytest.approx(0.9)
    assert proposed.effective_on == _DUE
    assert proposed.fit is not None
    assert proposed.fit.fitted_on == _DUE
    assert proposed.fit.input_sample == tuple(sorted(actuals))
    assert proposed.fit.residuals_eu == pytest.approx([0.0] * len(actuals))
    assert outcome.notice is not None
    assert f"revision {proposed.revision} applied" in outcome.notice
    assert outcome.options == ()


def test_meas_022_a_move_of_exactly_the_threshold_still_applies() -> None:
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 1.0)
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)
    assert outcome.relative_change == pytest.approx(0.25)
    assert outcome.disposition is RefitDisposition.APPLY


def test_meas_022_a_move_past_the_threshold_needs_an_operator_decision() -> None:
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 1.2)
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)

    assert outcome.disposition is RefitDisposition.NEEDS_DECISION
    assert outcome.relative_change == pytest.approx(0.5)
    assert outcome.notice is None
    assert outcome.question is not None
    assert "past the 25% threshold" in outcome.question
    assert [option.effect for option in outcome.options] == [
        OptionEffect.APPROVE,
        OptionEffect.DECLINE,
    ]
    assert all(option.consequence and option.cost for option in outcome.options)


def test_meas_023_excluded_rows_are_counted_per_reason() -> None:
    actuals = {
        **_actuals(3, 0.8),
        "X1": _actual("X1", 5.0, excluded=True),
        "X2": _actual("X2", 5.0, excluded=True),
        "Z1": _actual("Z1", 0.0),
        "A1": _actual("A1", 0.8, status=ActualStatus.ABANDONED),
    }
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)

    assert outcome.eligible_count == 3
    assert outcome.excluded == (
        ExclusionCount(reason=ExclusionReason.FLAGGED_EXCLUDED, count=2),
        ExclusionCount(reason=ExclusionReason.NOT_DONE, count=1),
        ExclusionCount(reason=ExclusionReason.NOT_POSITIVE, count=1),
    )


def test_meas_023_a_fit_records_its_exclusions() -> None:
    actuals = {
        **_actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 0.8),
        "X1": _actual("X1", 9.0, excluded=True),
    }
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)
    assert outcome.proposed is not None and outcome.proposed.fit is not None
    assert outcome.proposed.fit.excluded == (
        ExclusionCount(reason=ExclusionReason.FLAGGED_EXCLUDED, count=1),
    )
    assert "X1" not in outcome.proposed.fit.input_sample


def test_meas_016_045_a_refit_never_rewrites_an_estimate_or_the_mapping() -> None:
    estimate = _estimate()
    before = estimate.model_dump()
    digest = CURRENT_EFFORT_MAPPING.digest
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 0.9)

    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE)

    assert outcome.disposition is RefitDisposition.APPLY
    assert estimate.model_dump() == before
    assert estimate.mapping_revision == CURRENT_EFFORT_MAPPING.revision
    assert CURRENT_EFFORT_MAPPING.digest == digest
    assert outcome.current_digest == digest


def test_meas_020_the_fitted_dispersion_is_measured_in_minutes() -> None:
    actuals = {
        f"W{index:03d}": _actual(f"W{index:03d}", (index % 10 + 1) / 10) for index in range(100)
    }
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=date(2026, 12, 1))
    assert outcome.proposed is not None
    dispersion = outcome.proposed.dispersion
    assert dispersion.p10 <= dispersion.p50 <= dispersion.p90
    assert dispersion.p50 == pytest.approx(outcome.proposed.effort_eu * 30.0)


# --- MEAS-015 / MEAS-045: the stored revision in force ---------------------------------


def _applied(mapping: EffortMapping) -> LedgerRecord:
    row = AppliedMapping(
        mapping=mapping,
        digest=mapping.digest,
        request_ref="refit-1",
        actor="OP-0001",
        recorded_at=_T0,
        notice="applied",
    )
    return LedgerRecord(
        collection=Epoch2Collection.ESTIMATE,
        record_key=revision_record_key(mapping.revision, proposed=False),
        status="applied",
        recorded_at=_T0,
        payload=row.model_dump(mode="json"),
    )


def test_meas_015_no_stored_revision_reads_the_proposal_default() -> None:
    estimate_row = LedgerRecord(
        collection=Epoch2Collection.ESTIMATE,
        record_key="EST-1",
        status="history",
        recorded_at=_T0,
        payload={"kind": "estimate"},
    )
    assert mapping_in_force(()) is CURRENT_EFFORT_MAPPING
    assert mapping_in_force((estimate_row,)) is CURRENT_EFFORT_MAPPING


def test_meas_045_the_highest_stored_revision_is_in_force() -> None:
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 0.9)
    third = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE).proposed
    assert third is not None
    fourth = third.model_copy(update={"revision": 4})

    assert mapping_in_force((_applied(fourth), _applied(third))) == fourth
    assert revision_record_key(3, proposed=True) == "MAP-0003-PROPOSED"


def test_meas_022_the_same_fit_files_under_the_same_decision_key() -> None:
    actuals = _actuals(CURRENT_EFFORT_MAPPING.refit_minimum_sample, 1.2)
    first = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE).proposed
    later = refit_mapping(CURRENT_EFFORT_MAPPING, actuals, today=_DUE + timedelta(days=3)).proposed
    assert first is not None and later is not None
    assert proposal_key(CURRENT_EFFORT_MAPPING, first) == proposal_key(
        CURRENT_EFFORT_MAPPING, later
    )
    assert proposal_key(CURRENT_EFFORT_MAPPING, first).startswith("effort-refit-r3-")


def test_meas_023_unreadable_rows_are_counted_among_the_exclusions() -> None:
    outcome = refit_mapping(CURRENT_EFFORT_MAPPING, {}, today=_DUE, unreadable=2)
    assert outcome.excluded == (ExclusionCount(reason=ExclusionReason.UNREADABLE, count=2),)
