"""Empty-data and error contract for the ledger roll-up behind ``eawf metrics``.

:func:`compute_ledger_metrics` reads a generation's estimate and actual
ledgers. A born tree holds neither, so the roll-up must return its declared
empty shape -- zero counts and ``variance_pct is None`` -- rather than
raising or fabricating a ``0%`` reading. Boundary coverage runs empty ->
single -> two subjects; the error paths cover a subject with an actual but no
estimate, an excluded actual, a mapping revision filed on the estimate ledger
and a line that does not read as its kind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import ActualStatus, Confidence
from eawf.kernel.state.models import ActualSummary, EstimateSummary
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.workflow.estimation.metrics import (
    METRICS_SCHEMA_VERSION,
    EstimateActualVarianceMetric,
    LedgerMetrics,
    compute_ledger_metrics,
)

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _line(
    collection: Epoch2Collection, key: str, payload: dict[str, Any], **kw: Any
) -> LedgerRecord:
    return LedgerRecord(
        collection=collection,
        record_key=key,
        status=kw.get("status", "imported"),
        recorded_at=_T0,
        supersedes=None,
        payload=payload,
    )


def _estimate(key: str, expected_eu: float, *, revision: int | None = None) -> LedgerRecord:
    row = EstimateSummary(
        id=f"EST-{key}",
        scope_id=key,
        expected_eu=expected_eu,
        pessimistic_eu=expected_eu * 2,
        expected_minutes=expected_eu * 30,
        pessimistic_minutes=expected_eu * 60,
        display=f"{expected_eu} EU",
        current_store_record_id=f"EST-{key}-1",
        confidence=Confidence.MEDIUM,
        reference_class="bucket:M",
        mapping_revision=revision,
        updated_at=_T0,
    )
    return _line(Epoch2Collection.ESTIMATE, key, {"payload": row.model_dump(mode="json")})


def _actual(key: str, elapsed_eu: float, *, excluded: bool = False) -> LedgerRecord:
    row = ActualSummary(
        id=f"ACT-{key}",
        scope_id=key,
        elapsed_eu=elapsed_eu,
        status=ActualStatus.DONE,
        calibration_excluded=excluded,
        current_store_record_id=f"ACT-{key}-1",
        updated_at=_T0,
    )
    return _line(Epoch2Collection.ACTUAL, key, {"payload": row.model_dump(mode="json")})


def test_empty_ledgers_report_no_data() -> None:
    metrics = compute_ledger_metrics([], [])
    assert metrics == LedgerMetrics(
        estimate_count=0,
        actual_count=0,
        unreadable_count=0,
        variance=EstimateActualVarianceMetric(
            sample_count=0, planned_eu=0.0, actual_eu=0.0, variance_pct=None
        ),
    )
    assert metrics.schema_version == METRICS_SCHEMA_VERSION


def test_a_single_subject_reports_its_variance() -> None:
    metrics = compute_ledger_metrics([_estimate("T1", 2.0, revision=3)], [_actual("T1", 3.0)])
    assert (metrics.estimate_count, metrics.actual_count) == (1, 1)
    assert metrics.variance.sample_count == 1
    assert metrics.variance.variance_pct == pytest.approx(50.0)
    assert metrics.variance.mapping_revisions == ["3"]


def test_two_subjects_aggregate_and_label_an_unrecorded_revision() -> None:
    metrics = compute_ledger_metrics(
        [_estimate("T1", 1.0), _estimate("T2", 3.0, revision=2)],
        [_actual("T1", 1.0), _actual("T2", 1.0)],
    )
    assert metrics.variance.sample_count == 2
    assert metrics.variance.planned_eu == pytest.approx(4.0)
    assert metrics.variance.actual_eu == pytest.approx(2.0)
    assert metrics.variance.variance_pct == pytest.approx(-50.0)
    assert metrics.variance.mapping_revisions == ["2", "unrecorded"]


def test_an_actual_without_an_estimate_or_an_excluded_actual_does_not_contribute() -> None:
    metrics = compute_ledger_metrics(
        [_estimate("T1", 1.0)], [_actual("T1", 9.0, excluded=True), _actual("T2", 1.0)]
    )
    assert (metrics.estimate_count, metrics.actual_count) == (1, 2)
    assert metrics.variance.sample_count == 0
    assert metrics.variance.variance_pct is None


def test_a_native_line_reads_without_an_import_envelope() -> None:
    native = ActualSummary(
        id="ACT-T1",
        scope_id="T1",
        elapsed_eu=2.0,
        status=ActualStatus.DONE,
        current_store_record_id="ACT-T1-1",
        updated_at=_T0,
    )
    line = _line(Epoch2Collection.ACTUAL, "T1", native.model_dump(mode="json"), status="recorded")
    metrics = compute_ledger_metrics([_estimate("T1", 1.0)], [line])
    assert metrics.variance.variance_pct == pytest.approx(100.0)


def test_a_mapping_revision_is_no_estimate_and_a_bad_line_is_unreadable() -> None:
    mapping = _line(Epoch2Collection.ESTIMATE, "mapping-1", {"mapping": {}}, status="applied")
    garbage = _line(Epoch2Collection.ACTUAL, "T9", {"payload": {"elapsed_eu": "lots"}})
    metrics = compute_ledger_metrics([mapping], [garbage])
    assert (metrics.estimate_count, metrics.actual_count, metrics.unreadable_count) == (0, 0, 1)


def test_ledger_metrics_refuses_a_negative_count() -> None:
    with pytest.raises(ValidationError):
        LedgerMetrics(
            estimate_count=-1,
            actual_count=0,
            unreadable_count=0,
            variance=EstimateActualVarianceMetric(
                sample_count=0, planned_eu=0.0, actual_eu=0.0, variance_pct=None
            ),
        )
