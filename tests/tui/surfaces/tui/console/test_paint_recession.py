"""The painter's faint rows: a verb that cannot act, and a pane that does not own the arrows.

A refused verb stays listed with its reason and is drawn in the dim tone; a receded pane
loses its colours but keeps its structure, its heads and labels still bold.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.strip import Strip

from eawf.surfaces.tui.console.action_menu import Availability, Disabled, MenuVerb, menu_rows
from eawf.surfaces.tui.console.app import Body, ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.frame import LeadReceded, Receded
from eawf.surfaces.tui.console.paint import Part, Stroke, paint
from eawf.surfaces.tui.console.session import SessionSetup

from .overlay_support import prototype

_VERBS = (
    MenuVerb(key="y", verb="copy scope URN", available=True),
    MenuVerb(key="n", verb="new draft", available=False, reason="no daemon verb carries this yet"),
)


def _guard(verb: MenuVerb) -> Availability:
    return Availability(verb.available, verb.reason)


def _strokes(row: str) -> dict[str, Stroke]:
    return {s.text.strip(): s for s in paint(row, Part.BODY) if s.text.strip()}


# ---------- a verb that cannot act is drawn faint ----------


def test_a_refused_verb_row_is_disabled_and_an_available_one_is_not() -> None:
    head, available, refused = menu_rows(_VERBS, guard=_guard, w=120)
    assert not isinstance(head, Disabled) and not isinstance(available, Disabled)
    assert isinstance(refused, Disabled)
    assert "no daemon verb carries this yet" in refused, "the reason stays listed"
    assert len(refused) == 120


def test_a_disabled_row_is_one_dim_run() -> None:
    row = menu_rows(_VERBS, guard=_guard, w=120)[2]
    assert [(s.surface, s.bold) for s in paint(row, Part.BODY)] == [("dim", False)]


def test_an_available_verb_row_keeps_the_text_colour() -> None:
    row = menu_rows(_VERBS, guard=_guard, w=120)[1]
    assert all(s.surface is None for s in paint(row, Part.BODY))


def test_a_refused_verb_row_stays_dim_in_the_live_drawer() -> None:
    """The drawer frame keeps the mark, so the running console paints the row faint."""

    async def body_rows() -> tuple[list[str], list[Strip]]:
        app = ConsoleApp(prototype(), clock=FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            app.reset(SessionSetup(route="run.detail", subjId="RUN-538453eb"))
            app.render_frame()
            await pilot.press("full_stop")
            await pilot.pause()
            body = app.query_one("#body", Body)
            return list(body.rows), [body.render_line(y) for y in range(len(body.rows))]

    rows, strips = asyncio.run(body_rows())

    def colours(y: int) -> set[object]:
        return {seg.style.color for seg in strips[y] if seg.text.strip() and seg.style}

    refused = [y for y, row in enumerate(rows) if isinstance(row, Disabled)]
    assert refused, "a refused verb row reaches the widget still marked"
    head = next(y for y, row in enumerate(rows) if row.startswith("   ACTIONS   KEY"))
    for y in refused:
        assert len(colours(y)) == 1, "the whole refused row is one faint run"
        assert colours(y) != colours(head), "the refused row is not in the text colour"


def test_the_menu_refuses_a_width_narrower_than_its_columns() -> None:
    with pytest.raises(ValueError, match="needs"):
        menu_rows(_VERBS, guard=_guard, w=20)


# ---------- the pane that does not own the arrows recedes ----------


def test_a_receded_row_keeps_its_pane_label_and_heads_bold() -> None:
    label = _strokes(Receded("EXECUTION        │  PLANNING           global › repo"))  # noqa: RUF001
    assert (label["EXECUTION"].surface, label["EXECUTION"].bold) == ("recede", True)
    assert label["PLANNING"].bold
    heads = _strokes(Receded("  agents         │     KEY        VALUE        FROM"))
    head = heads["KEY        VALUE        FROM"]
    assert (head.surface, head.bold) == ("recede", True)


def test_a_receded_row_takes_no_accent() -> None:
    strokes = paint(Receded("▸ planning       │ ▸ – approval     ask"), Part.BODY)  # noqa: RUF001
    assert {s.surface for s in strokes} == {"recede"}
    assert not any(s.ground for s in strokes)


@pytest.mark.parametrize(
    "row", ["SAFETY           │  ▸ ● ask", "  verify         ├──────────────────────────"]
)
def test_a_lead_receded_row_recedes_only_its_rail(row: str) -> None:
    strokes = paint(LeadReceded(row), Part.BODY)
    edge = next(i for i, ch in enumerate(row) if ch in "│├")
    at = 0
    for stroke in strokes:
        if stroke.text.strip():
            assert (stroke.surface == "recede") == (at < edge), stroke
        at += len(stroke.text)
