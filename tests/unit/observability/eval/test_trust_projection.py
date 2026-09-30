"""UI-020, UI-063: the TrustView groups hold no number they cannot stand behind.

An ``INSUFFICIENT`` calibration report carries no numeric field and the gate refuses on
the cohort; a ``SCORED`` one earns blocking authority only when its Brier score and its
co-error rate both clear their ceilings, and otherwise names the metric that refused; a
producer that judged nothing has no rate rather than a zero one; and the
verdicts are scoped to the one Milestone the view was asked for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.verification import build_verification_view
from eawf.observability.eval.jury_validation import (
    JuryValidationConfig,
    JuryValidationReport,
    JuryValidationStatus,
    ValidationCohort,
)
from eawf.observability.eval.trust_projection import (
    TrackRecordRow,
    build_trust_view,
    calibrate,
    calibration_authority,
)

ROOT = "eawf://EAWF/EAWF/EAWF"


def _observation(subject: str, verdict: str, milestone: str, role: str | None) -> dict[str, Any]:
    return {
        "payload_kind": "verdict_observation",
        "urn": f"{ROOT}/batch/BAT-0001",
        "revision": 1,
        "status": verdict,
        "site": "verification",
        "subject": subject,
        "batch_ref": f"{ROOT}/batch/BAT-0001",
        "milestone_ref": f"{ROOT}/milestone/{milestone}",
        "verdict": verdict,
        "agent_role": role,
        "runtime": "codex" if role else None,
        "occurred_at": "2026-09-30T10:00:00+00:00",
    }


def _model(*rows: dict[str, Any]) -> Any:
    document = {"batch": {f"OBS-{i}": row for i, row in enumerate(rows)}}
    projection = build_route_projection(
        route="trust",
        document=document,
        cursor=1,
        scope_id="EAWF",
        generated_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    return build_verification_view(projection)


def test_ui_020_an_insufficient_report_carries_no_number_and_refuses_on_the_cohort() -> None:
    group = calibrate(
        ValidationCohort(silver=[], gold=[]),
        {},
        max_brier=0.25,
        max_co_error=0.10,
        config=JuryValidationConfig(min_validation_n=5),
    )
    report = group.report
    assert report.status is JuryValidationStatus.INSUFFICIENT
    assert (report.n, group.min_scored) == (0, 5)
    assert (report.brier, report.ece, report.fleiss_kappa) == (None, None, None)
    assert report.unanimous_pass_on_known_bad_rate is None
    assert group.authority == "refused · n"


def _scored(*, brier: float | None, co_error: float | None) -> JuryValidationReport:
    return JuryValidationReport(
        n=20,
        status=JuryValidationStatus.SCORED,
        brier=brier,
        unanimous_pass_on_known_bad_rate=co_error,
        known_bad_n=0 if co_error is None else 2,
    )


@pytest.mark.parametrize(
    ("report", "authority"),
    [
        (_scored(brier=0.0, co_error=0.0), "blocking · earned"),
        (_scored(brier=0.25, co_error=0.10), "blocking · earned"),
        (_scored(brier=0.26, co_error=0.0), "refused · Brier"),
        (_scored(brier=None, co_error=0.0), "refused · Brier"),
        (_scored(brier=0.1, co_error=0.11), "refused · co-error"),
        (_scored(brier=0.1, co_error=None), "refused · co-error"),
        (_scored(brier=0.9, co_error=0.9), "refused · Brier"),
        (
            JuryValidationReport(n=19, status=JuryValidationStatus.INSUFFICIENT, known_bad_n=3),
            "refused · n",
        ),
    ],
    ids=[
        "clears",
        "at-both-ceilings",
        "brier-over",
        "brier-undefined",
        "co-error-over",
        "no-known-bad",
        "first-failing-metric-named",
        "insufficient",
    ],
)
def test_ui_063_the_gate_earns_blocking_only_when_every_threshold_clears(
    report: JuryValidationReport, authority: str
) -> None:
    assert calibration_authority(report, max_brier=0.25, max_co_error=0.10) == authority


def _calibration(**fields: Any) -> dict[str, Any]:
    return {
        "payload_kind": "jury_calibration",
        "urn": f"{ROOT}/repository/REP-EAWF",
        "revision": 1,
        **fields,
    }


def test_ui_020_the_view_reads_the_calibration_row_and_never_lists_it_as_a_verdict() -> None:
    document = {
        "batch": {
            "OBS-0": _observation("CR-01", "verified_true", "MLS-0001", "auditor"),
            "jury-calibration": _calibration(
                status="scored",
                cohort=22,
                known_bad=2,
                min_scored=20,
                brier=0.0,
                co_error=0.0,
                authority="blocking · earned",
            ),
        }
    }
    projection = build_route_projection(
        route="trust",
        document=document,
        cursor=1,
        scope_id="EAWF",
        generated_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    view = build_trust_view(build_verification_view(projection), milestone=None)

    assert [row.key for row in view.verdicts] == ["OBS-0"]
    report = view.calibration.report
    assert report.status is JuryValidationStatus.SCORED
    assert (report.n, report.known_bad_n, view.calibration.min_scored) == (22, 2, 20)
    assert report.brier == pytest.approx(0.0)
    assert report.unanimous_pass_on_known_bad_rate == pytest.approx(0.0)
    assert view.calibration.authority == "blocking · earned"


def test_ui_020_an_undefined_metric_on_the_calibration_row_reads_absent() -> None:
    document = {
        "batch": {
            "jury-calibration": _calibration(
                status="insufficient",
                cohort=3,
                known_bad=0,
                min_scored=20,
                brier=None,
                co_error=None,
                authority="refused · n",
            )
        }
    }
    projection = build_route_projection(
        route="trust",
        document=document,
        cursor=1,
        scope_id="EAWF",
        generated_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    group = build_trust_view(build_verification_view(projection), milestone=None).calibration

    assert (group.report.n, group.report.brier) == (3, None)
    assert group.report.unanimous_pass_on_known_bad_rate is None
    assert group.authority == "refused · n"


def test_ui_020_no_calibration_row_reads_the_empty_cohort_refused() -> None:
    group = build_trust_view(_model(), milestone=None).calibration
    assert group.report.status is JuryValidationStatus.INSUFFICIENT
    assert (group.report.n, group.authority) == (0, "refused · n")


@pytest.mark.parametrize(
    ("accepted", "rejected", "rate"), [(0, 0, None), (1, 0, 1.0), (1, 3, 0.25)]
)
def test_ui_020_a_rate_over_zero_judged_is_undefined_never_zero(
    accepted: int, rejected: int, rate: float | None
) -> None:
    row = TrackRecordRow(
        agent_role="auditor", runtime="codex", accepted=accepted, rejected=rejected
    )
    assert row.judged == accepted + rejected
    assert row.rate == (None if rate is None else pytest.approx(rate))


def test_ui_063_the_view_scopes_verdicts_to_its_milestone_and_tallies_producers() -> None:
    model = _model(
        _observation("CR-01", "verified_true", "MLS-0001", "auditor"),
        _observation("CR-02", "unverified", "MLS-0001", None),
        _observation("CR-03", "verified_false", "MLS-0002", "auditor"),
    )

    view = build_trust_view(model, milestone="MLS-0001")

    assert [row.field("subject").value for row in view.verdicts] == ["CR-01", "CR-02"]
    by_producer = {(row.agent_role, row.runtime): row for row in view.track_record}
    assert (
        by_producer[("auditor", "codex")].accepted,
        by_producer[("auditor", "codex")].rejected,
    ) == (1, 0)
    unjudged = by_producer[("? unknown", "? unknown")]
    assert unjudged.judged == 0 and unjudged.rate is None


def test_ui_063_no_milestone_named_lists_every_verdict() -> None:
    model = _model(
        _observation("CR-01", "verified_true", "MLS-0001", "auditor"),
        _observation("CR-03", "verified_false", "MLS-0002", "auditor"),
    )
    assert len(build_trust_view(model, milestone=None).verdicts) == 2


def test_ui_063_an_empty_projection_yields_empty_groups() -> None:
    view = build_trust_view(_model(), milestone="MLS-0001")
    assert (view.verdicts, view.unobserved, view.track_record) == ((), (), ())
