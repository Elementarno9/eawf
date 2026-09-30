"""UI-020, UI-063: the TrustView groups hold no number they cannot stand behind.

An ``INSUFFICIENT`` calibration report carries no numeric field and the gate refuses on
the cohort; a producer that judged nothing has no rate rather than a zero one; and the
verdicts are scoped to the one Milestone the view was asked for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.verification import build_verification_view
from eawf.observability.eval.jury_validation import JuryValidationConfig, JuryValidationStatus
from eawf.observability.eval.trust_projection import (
    TrackRecordRow,
    build_trust_view,
    calibrate,
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
    group = calibrate(JuryValidationConfig(min_validation_n=5))
    report = group.report
    assert report.status is JuryValidationStatus.INSUFFICIENT
    assert (report.n, group.min_scored) == (0, 5)
    assert (report.brier, report.ece, report.fleiss_kappa) == (None, None, None)
    assert report.unanimous_pass_on_known_bad_rate is None
    assert group.authority == "refused · n"


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
