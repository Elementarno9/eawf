"""A windowed region is never under three rows and spends its edge counts from its own rows.

CON-162: every windowable region has a minimum height of three rows, spends its
indicators from its own row budget, follows its own cursor when focused and shows its
head when not; a region states its extent once, as a range line or as edge counts, never
both. The campaign route's three sections at 80x24 are the densest case: every section is
at the minimum there, so a cap cut to two rows or an indicator added on top of the cap
shows up at once.
"""

from __future__ import annotations

import re

import pytest

from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import MIN_WINDOW, Breadth, View, window_rows
from eawf.surfaces.tui.console.renderers.campaign import ARTIFACTS, EVIDENCE, PLAN, caps, window
from eawf.surfaces.tui.console.session import Session, SessionSetup
from tests.tui.surfaces.tui.console.test_chrome_sweeps import (
    GOLDEN_ROOT,
    RENDERED,
    body_rows,
    render_state,
)

SECTIONS = (PLAN, EVIDENCE, ARTIFACTS)
_EDGE = re.compile(r"… \d[\d,]* (?:above|below|earlier|later)\b")
_RANGE = re.compile(r"\bWINDOW +\d[\d,]*[–-]\d[\d,]* of\b")  # noqa: RUF001


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    return load_fixture(GOLDEN_ROOT / "fixture")


def _section_windows(rows: list[str]) -> dict[str, list[str]]:
    """Return each campaign section's window rows: everything after its head row."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    head_seen = False
    for row in body_rows(rows):
        label = row[1:13].strip()
        if label in SECTIONS:
            current, head_seen = label, False
            out[current] = []
            continue
        if current is None:
            continue
        if set(row.strip()) <= {"─"} or row[1:13].strip():
            current = None
            continue
        if not head_seen:
            head_seen = True
            continue
        out[current].append(row)
    return out


def test_con_162_the_campaign_sections_at_80x24_are_each_three_rows() -> None:
    windows = _section_windows(RENDERED["route/campaign@80"])
    assert set(windows) == set(SECTIONS)
    for name, region in windows.items():
        assert len(region) == MIN_WINDOW == caps(Breadth.NARROW)[name], (name, region)
        assert _EDGE.search(region[-1]), (name, region)


@pytest.mark.parametrize("breadth", list(Breadth))
def test_con_162_no_campaign_section_cap_is_under_the_minimum(breadth: Breadth) -> None:
    assert min(caps(breadth).values()) >= MIN_WINDOW


@pytest.mark.parametrize("cap", [3, 4, 6])
@pytest.mark.parametrize("total", [0, 1, 2, 3, 4, 5, 9, 26])
@pytest.mark.parametrize("focused", [True, False])
def test_con_162_a_window_spends_its_edge_counts_from_its_own_rows(
    cap: int, total: int, focused: bool
) -> None:
    for sel in range(max(total, 1)):
        win = window(total, cap, sel, focused)
        drawn = win.take + bool(win.above) + bool(win.below)
        assert drawn == min(total, cap), (sel, win)
        assert win.above == win.start
        assert win.below == total - win.start - win.take
        assert win.above != 1 and win.below != 1, (sel, win)
        if focused and total:
            assert win.start <= sel < win.start + win.take, (sel, win)
        else:
            assert win.start == 0


def test_con_162_the_focused_section_follows_its_cursor_and_the_others_show_their_head(
    fixture: Fixture,
) -> None:
    rows = render_state(fixture, SessionSetup(route="campaign"), ["ArrowDown"] * 4)
    windows = _section_windows(rows)
    plan = windows[PLAN]
    assert any(row.lstrip().startswith("▸ 5 cross-provider") for row in plan), plan
    assert plan[0].strip().startswith("… ") and "above" in plan[0]
    assert "EVD-0011" in windows[EVIDENCE][0]
    assert "drift-report.md" in windows[ARTIFACTS][0]


def test_con_162_a_table_window_never_shrinks_under_the_minimum() -> None:
    session = Session()
    view = View(session=session, fixture=load_fixture(GOLDEN_ROOT / "fixture"), w=80, h=6)
    win = window_rows(view, total=100, cursor=50, chrome=10)
    assert win.stop - win.start == MIN_WINDOW
    assert win.start <= 50 < win.stop


def test_con_162_no_frame_states_a_range_line_beside_edge_counts() -> None:
    both = {
        fid: [row.strip() for row in body_rows(rows) if _RANGE.search(row) or _EDGE.search(row)]
        for fid, rows in RENDERED.items()
        if any(_RANGE.search(r) for r in rows) and any(_EDGE.search(r) for r in rows)
    }
    assert both == {}


def test_con_162_the_extent_sweep_reds_on_a_doubled_extent() -> None:
    rows = [" h", " WINDOW    2–24 of 26", "   … 1 above", " k"]  # noqa: RUF001
    assert any(_RANGE.search(r) for r in rows) and any(_EDGE.search(r) for r in rows)
