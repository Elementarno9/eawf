"""Inline markdown in console titles and criteria draws its words, never its markup.

The parser keeps the visible text a frame measures and carries each style as a span; a
row laid out to the frame keeps the spans that still fall on it, and the painter draws
them: code in its own surface, bold, italic, and links underlined.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import Fixed, View
from eawf.surfaces.tui.console.inline import Inline, Marked, Span, inline_markdown, lay, prefixed
from eawf.surfaces.tui.console.paint import Part, Stroke, paint
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.children import named
from eawf.surfaces.tui.console.rows import RowsWidget
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.token_map import SURFACES

AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
ROOT = "eawf://EAWF/EAWF/EAWF"
CRITERION = "The median wall time of the last 10 PR runs of `ci.yaml` is at or under 15 min."


def _styled(marked: Marked) -> list[tuple[str, Inline]]:
    return [(marked[s.start : s.end], s.style) for s in marked.spans]


# ---------- the parser ----------


@pytest.mark.parametrize(
    ("text", "plain", "styled"),
    [
        ("a **bold** b", "a bold b", [("bold", Inline.BOLD)]),
        ("a *it* b", "a it b", [("it", Inline.ITALIC)]),
        ("a _it_ b", "a it b", [("it", Inline.ITALIC)]),
        ("run `ci.yaml` now", "run ci.yaml now", [("ci.yaml", Inline.CODE)]),
        ("see [docs](https://x.test/a) here", "see docs here", [("docs", Inline.LINK)]),
        ("<https://x.test>", "https://x.test", [("https://x.test", Inline.LINK)]),
    ],
)
def test_each_inline_style_draws_its_words_and_one_span(
    text: str, plain: str, styled: list[tuple[str, Inline]]
) -> None:
    marked = inline_markdown(text)
    assert marked == plain
    assert _styled(marked) == styled


def test_nested_bold_and_code_carry_both_spans_over_the_code() -> None:
    marked = inline_markdown("a **b `c` d** e")
    assert marked == "a b c d e"
    assert sorted(_styled(marked)) == [("b c d", Inline.BOLD), ("c", Inline.CODE)]


@pytest.mark.parametrize(
    "text",
    [
        "un `matched backtick",
        "[brackets] are text, not markup",
        "READY_TO_INTEGRATE and snake_case_name",
        "2 * 3 * 4",
        "plain words",
    ],
)
def test_text_markdown_does_not_close_stays_literal(text: str) -> None:
    marked = inline_markdown(text)
    assert marked == text
    assert marked.spans == ()


def test_an_escaped_asterisk_draws_the_asterisk_and_no_emphasis() -> None:
    marked = inline_markdown(r"\*not italic\*")
    assert marked == "*not italic*"
    assert marked.spans == ()


def test_an_empty_text_is_an_empty_unstyled_row() -> None:
    marked = inline_markdown("")
    assert marked == ""
    assert marked.spans == ()


def test_a_single_styled_character_is_one_span() -> None:
    assert _styled(inline_markdown("`x`")) == [("x", Inline.CODE)]


def test_a_soft_break_reads_as_one_space() -> None:
    assert inline_markdown("one\ntwo") == "one two"


def test_markup_with_no_visible_text_leaves_no_empty_span() -> None:
    marked = inline_markdown("a [](https://x.test) b")
    assert marked == "a  b"
    assert marked.spans == ()


@pytest.mark.parametrize("bad", [None, 3])
def test_a_non_string_is_refused(bad: Any) -> None:
    with pytest.raises(TypeError):
        inline_markdown(bad)


# ---------- prefixing and laying out keep the spans on their words ----------


def test_a_prefix_moves_every_span_past_it() -> None:
    row = prefixed(" LABEL  ", inline_markdown("x **y**"))
    assert row == " LABEL  x y"
    assert _styled(row) == [("y", Inline.BOLD)]


def test_an_empty_prefix_keeps_the_spans_in_place() -> None:
    value = inline_markdown("`z`")
    assert prefixed("", value).spans == value.spans


def test_a_row_that_fits_is_padded_and_keeps_its_spans() -> None:
    row = lay(prefixed("  ", inline_markdown("a `b`")), 10)
    assert row == "  a b     "
    assert _styled(row) == [("b", Inline.CODE)]
    assert isinstance(row, Fixed)


def test_a_row_exactly_the_width_is_not_clipped() -> None:
    row = lay(inline_markdown("ab `cd`"), 5)
    assert row == "ab cd"
    assert _styled(row) == [("cd", Inline.CODE)]


def test_a_row_one_cell_too_wide_is_clipped_and_its_span_cut_at_the_ellipsis() -> None:
    row = lay(inline_markdown("a `bcdefgh`"), 6)
    assert row == "a bcd…"
    assert _styled(row) == [("bcd", Inline.CODE)]
    assert lay(inline_markdown("a `bcdef`"), 6) == "a bcd…"
    assert lay(inline_markdown("a `bcde`"), 6) == "a bcde"


def test_a_span_wholly_past_the_clip_is_dropped() -> None:
    row = lay(inline_markdown("a long run of words then `code`"), 12)
    assert "code" not in row
    assert row.spans == ()


def test_a_zero_width_row_is_empty_and_unstyled() -> None:
    row = lay(inline_markdown("**b**"), 0)
    assert row == ""
    assert row.spans == ()


def test_a_negative_width_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        lay(inline_markdown("x"), -1)


# ---------- the painter draws each span ----------


def _run(strokes: tuple[Stroke, ...], text: str) -> Stroke:
    return next(stroke for stroke in strokes if text in stroke.text)


def test_the_code_surface_is_in_the_token_map() -> None:
    assert SURFACES["code"].token == "primary"


def test_the_painter_draws_code_bold_italic_and_link_spans() -> None:
    row = lay(inline_markdown("`c1` **b1** *i1* [l1](https://x.test)"), 30)
    strokes = paint(row, Part.BODY)
    assert "".join(stroke.text for stroke in strokes) == row
    assert _run(strokes, "c1").surface == "code"
    assert _run(strokes, "b1").bold
    assert _run(strokes, "i1").italic
    assert _run(strokes, "l1").underline
    assert not _run(strokes, "c1").italic


def test_a_plain_row_paints_no_italic() -> None:
    assert not any(stroke.italic for stroke in paint(" plain words", Part.BODY))


def test_a_stroke_refuses_a_surface_the_token_map_lacks() -> None:
    with pytest.raises(ValueError, match="not in the token map"):
        Stroke("x", surface="nope", italic=True)


# ---------- wired: the Task frame draws a criterion and a title ----------


def _urn(kind: str, key: str) -> str:
    return f"{ROOT}/{kind}/{key}"


DOCUMENT: dict[str, Any] = {
    "milestone": {
        "MLS-0100": {
            "key": "MLS-0100",
            "urn": _urn("milestone", "MLS-0100"),
            "revision": 1,
            "status": "ACTIVE",
            "title": "Ship **rc2**",
            "target_date": "2026-10-10",
        }
    },
    "task": {
        "TSK-0001": {
            "key": "TSK-0001",
            "urn": _urn("task", "TSK-0001"),
            "revision": 1,
            "status": "RUNNING",
            "intent": "Keep CI fast",
            "due_scope": _urn("milestone", "MLS-0100"),
            "criteria": [
                {"id": "CR-001", "text": CRITERION, "kind": "behavioral", "gate_ids": ["G-01"]}
            ],
        },
    },
}


def _projection(route: str) -> Any:
    return build_route_projection(
        route=route, document=DOCUMENT, cursor=1, scope_id="EAWF", generated_at=AT
    )


def _task_frame() -> list[str]:
    projection = _projection("task.detail")
    session = Session()
    session.route = "task.detail"
    session.subj_id = "TSK-0001"
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=40,
        projection=build_spine_view(projection),
        # the Milestone a Task is due with is read off the scope's rows, not the Task's
        rows=tuple(_projection("scope.home").rows),
    )
    return render_route(view)


def test_a_criterion_row_draws_its_code_in_the_code_surface_and_no_backticks() -> None:
    frame = _task_frame()
    assert not any("`" in row for row in frame)
    row = next(row for row in frame if "ci.yaml" in row)
    assert isinstance(row, Marked)
    assert len(row) == 160
    assert "of ci.yaml is at or under 15 min." in row
    assert _run(paint(row, Part.BODY), "ci.yaml").surface == "code"
    assert _run(paint(row, Part.BODY), "CR-001").surface != "code"


def test_a_milestone_title_draws_bold_in_the_due_row() -> None:
    frame = _task_frame()
    row = next(row for row in frame if "with MLS-0100" in row)
    assert "**" not in row
    assert "2026-10-10 · with MLS-0100 Ship rc2" in row
    assert isinstance(row, Marked)
    assert _run(paint(row, Part.BODY), "rc2").bold


@dataclass(frozen=True)
class _Record:
    """A Milestone record as :func:`named` reads it."""

    title: str | None
    key: str = "MLS-0001"
    parent_key: str | None = None
    collection: Epoch2Collection = Epoch2Collection.MILESTONE

    def field(self, name: str) -> TruthField[str]:
        raise KeyError(name)


def test_named_draws_a_bold_title_after_its_key() -> None:
    marked = named(_Record("A **big** step"))
    assert marked == "MLS-0001 A big step"
    assert marked.spans == (Span(11, 14, Inline.BOLD),)


def test_named_without_a_title_is_the_key_alone() -> None:
    marked = named(_Record(None))
    assert marked == "MLS-0001"
    assert marked.spans == ()


def test_named_interpolated_into_text_keeps_only_the_drawn_words() -> None:
    assert f"{named(_Record('`x`'))} · ACTIVE" == "MLS-0001 x · ACTIVE"


# ---------- the widget repaints a row whose markup alone changed ----------


def test_a_row_whose_spans_alone_changed_is_repainted(monkeypatch: pytest.MonkeyPatch) -> None:
    widget = RowsWidget()
    refreshed: list[tuple[object, ...]] = []
    monkeypatch.setattr(widget, "refresh", lambda *regions: refreshed.append(regions))
    widget.set_rows([lay(inline_markdown("a b"), 3)])
    refreshed.clear()
    widget.set_rows([lay(inline_markdown("a `b`"), 3)])
    assert len(refreshed) == 1 and len(refreshed[0]) == 1
    refreshed.clear()
    widget.set_rows([lay(inline_markdown("a `b`"), 3)])
    assert refreshed == []
