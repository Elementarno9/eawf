"""Budgets, progress and finished entities render as facts, never as countdowns or alarms.

Each test names the packet row it proves. CON-077: a budget is spent, limit and an
estimated remainder, and the time basis is elapsed, limit and a derived typical value,
never a remaining time. CON-078: progress is a named numerator with its marker, never a
bare percentage. CON-081: a finished entity drops the connection line, says nothing is
wrong, ages informationally and offers no lifecycle control.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.spine import SpineView, build_spine_view
from eawf.kernel.projection.truth import Freshness, Precision, TruthField, TruthKind, TruthState
from eawf.kernel.state.enums import MeasurementQuality
from eawf.surfaces.tui.console.cells import spans
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.harness import Contract, load_contract
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.normalisation import Normaliser, load_map
from eawf.surfaces.tui.console.plain import render_plain
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.spine import finished_subject
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import CONNECTION

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"

#: A remaining time: an estimate of time left, which the time basis never renders.
COUNTDOWN = re.compile(r"≈\d+(?:\.\d+)?[smh] left")
#: A percentage standing without a named numerator beside it.
BARE_PERCENT = re.compile(r"\d+%(?! · \d+ of \d+)")


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the tracked prototype registers."""
    return load_fixture(GOLDEN_ROOT / "fixture")


@pytest.fixture(scope="module")
def contract() -> Contract:
    """Return the tracked golden contract."""
    return load_contract(GOLDEN_ROOT / "sequences")


@pytest.fixture(scope="module")
def expected(contract: Contract) -> dict[str, list[str]]:
    """Return every recorded frame as the port is expected to render it, by id."""
    normaliser = Normaliser(
        load_map(GOLDEN_ROOT / "normalisation-map.json"), contract.frames_by_id()
    )
    frames = {s.id: normaliser.expected(s.id, s.frame).split("\n") for s in contract.states}
    for journey in contract.journeys:
        for i, step in enumerate(journey.steps):
            frames[f"{journey.id}#{i}"] = normaliser.expected(journey.id, step.frame).split("\n")
    return frames


def _rows(fixture: Fixture, route: str, size: int = 0) -> list[str]:
    """Return the plain frame the prototype registers draw for ``route``."""
    return render_plain(fixture, SessionSetup(route=route, size=size), conn="LIVE")


# ---------- CON-077: a budget is a fact, not a countdown ----------


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_077_the_cost_ceiling_states_spent_limit_and_an_estimated_remainder(
    fixture: Fixture, size: int
) -> None:
    rows = _rows(fixture, "cost.ceiling", size)
    ceiling = next(row for row in rows if row.startswith(" CEILING"))
    assert "~41.20 of 60.00 today" in ceiling
    assert "^18.80 left" in ceiling  # the estimated remainder, in the ASCII allocation
    assert not any("%" in row or "#" * 3 in row for row in rows[:-1])


def test_con_077_the_run_usage_states_elapsed_limit_and_typical_never_time_left(
    fixture: Fixture,
) -> None:
    rows = _rows(fixture, "run.detail")
    usage = rows[rows.index(next(r for r in rows if r.startswith(" USAGE"))) :][:3]
    assert "elapsed 18m 04s of 60m" in usage[0]
    assert "typical ~22m" in usage[0]
    assert "cost ~4.62 of 20.00 - ^15.38 left" in usage[1]
    assert not any(re.search(r"\^\d+m left", row) for row in usage)


def test_con_077_the_checking_phase_states_its_typical_duration(fixture: Fixture) -> None:
    rows = _rows(fixture, "batch.detail")
    checking = next(row for row in rows if row.startswith("   checking"))
    assert "typical ~6m" in checking
    assert "left" not in checking


def test_con_077_no_expected_frame_renders_a_remaining_time(
    expected: dict[str, list[str]],
) -> None:
    counted = [fid for fid, rows in expected.items() if any(COUNTDOWN.search(r) for r in rows)]
    assert counted == []
    assert any(
        "typical ~6m" in row
        for fid, rows in expected.items()
        if fid.startswith("J24#")
        for row in rows
    )


# ---------- CON-078: progress is a named numerator ----------


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_con_078_queue_progress_is_a_named_numerator_with_its_marker(
    fixture: Fixture, size: int
) -> None:
    rows = render_plain(fixture, SessionSetup(route="unattended", size=size), conn="LIVE")
    running = [row for row in rows if "RUNNING" in row]
    assert len(running) == 2
    for row in running:
        assert re.search(r"RUNNING +~\d+ of \d+ steps", row), row
        assert "%" not in row


def test_con_078_no_expected_frame_renders_a_bare_percentage(
    expected: dict[str, list[str]],
) -> None:
    bare = [fid for fid, rows in expected.items() if any(BARE_PERCENT.search(r) for r in rows)]
    assert bare == []
    named = [row for rows in expected.values() for row in rows if "%" in row]
    assert named  # the card still shows its percentage, beside the numerator it names


def test_con_078_the_numerator_wears_its_quality_marker(fixture: Fixture) -> None:
    rows = render_plain(fixture, SessionSetup(route="unattended"), conn="LIVE")
    assert any("~5 of 8 steps" in row for row in rows)


# ---------- CON-081: a finished entity says nothing is wrong ----------


def _status(value: str | None, state: TruthState = TruthState.KNOWN) -> TruthField[str]:
    known = state is TruthState.KNOWN
    return TruthField[str](
        value=value if known else None,
        state=state,
        truth_kind=TruthKind.STORED,
        producer="document",
        producer_revision=1,
        precision=Precision.EXACT if known else Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.EXACT if known else MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=("urn:probe:1",) if known else (),
        missing_reason=None if known else "producer silent",
    )


def _run_spine(status: TruthField[str], *, key: str = "RUN-9e3779b1") -> SpineView:
    document: dict[str, Any] = {
        "run": {key: {"urn": f"urn:eawf:{SCOPE}:run:{key}", "revision": 7, "status": "RUNNING"}},
    }
    spine = build_spine_view(
        build_route_projection(
            route="run.detail", document=document, cursor=41208, scope_id=SCOPE, generated_at=AT
        )
    )
    rows = tuple(
        dataclasses.replace(row, fields={**row.fields, "status": status}) for row in spine.rows
    )
    return dataclasses.replace(spine, rows=rows)


def _frame(fixture: Fixture, spine: SpineView, *, subject: str | None, conn: str) -> list[str]:
    session = Session()
    session.route = "run.detail"
    session.subj_id = subject
    session.conn = conn
    return render_route(View(session=session, fixture=fixture, w=120, h=30, projection=spine))


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED"])
@pytest.mark.parametrize("conn", ["LIVE", "DISCONNECTED"])
def test_con_081_a_finished_run_says_nothing_is_wrong(
    fixture: Fixture, status: str, conn: str
) -> None:
    rows = _frame(fixture, _run_spine(_status(status)), subject="RUN-9e3779b1", conn=conn)
    body = rows[1:-1]
    final = next(row for row in body if row.startswith(" FINAL"))
    assert f"RUN-9e3779b1 {status} · finished, nothing is wrong" in final
    assert any("ages informationally after 30s" in row for row in body)
    assert not any(row.startswith(" ATTACHED") for row in body)
    glyphs = {glyph.unicode for glyph in CONNECTION.values()}
    assert not any(ch in glyphs for row in body[2:] for ch in row)
    # the Run's lifecycle is over, but its menu still opens its Git and its transcript
    assert " ".join(KEY["actions"].pair()) in rows[-1]


def test_con_081_the_finished_rows_carry_no_truth_mark(fixture: Fixture) -> None:
    rows = _frame(fixture, _run_spine(_status("FAILED")), subject="RUN-9e3779b1", conn="LIVE")
    final = [row for row in rows if row.startswith(" FINAL") or "informationally" in row]
    assert len(final) == 2
    assert not any(span.mark for row in final for span in spans(row))


def test_con_081_a_running_subject_keeps_its_connection_line_and_controls(
    fixture: Fixture,
) -> None:
    rows = _frame(
        fixture, _run_spine(_status("RUNNING")), subject="RUN-9e3779b1", conn="DISCONNECTED"
    )
    assert not any(row.startswith(" FINAL") for row in rows)
    assert any(row.startswith(" ATTACHED") for row in rows)
    assert " ".join(KEY["actions"].pair()) in rows[-1]


@pytest.mark.parametrize(
    ("status", "subject"),
    [
        (_status("COMPLETED"), None),
        (_status("COMPLETED"), "RUN-00000000"),
        (_status(None, TruthState.UNKNOWN), "RUN-9e3779b1"),
        (_status("QUEUED"), "RUN-9e3779b1"),
    ],
    ids=["no subject", "subject not held", "status unknown", "not terminal"],
)
def test_con_081_only_a_held_finished_subject_is_finished(
    status: TruthField[str], subject: str | None
) -> None:
    session = Session()
    session.subj_id = subject
    assert finished_subject(session, _run_spine(status)) is None


def test_con_081_a_finished_subject_is_its_row() -> None:
    session = Session()
    session.subj_id = "RUN-9e3779b1"
    found = finished_subject(session, _run_spine(_status("COMPLETED")))
    assert found is not None
    assert found.key == "RUN-9e3779b1"
