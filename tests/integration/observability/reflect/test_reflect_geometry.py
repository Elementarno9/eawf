"""Browser geometry probe: the reflect pages drawn by Chromium, judged by their rects.

The probe opens the overview, the finder and a session page at 900, 1280 and
1600px wide in light and dark, and fails on any element but the page that
scrolls vertically, any text more than its slack past the box that clips it,
and any two text or mark boxes in one ``[data-chart]`` root that overlap. It
drives Chromium through Playwright, which the repository does not install: the
browser leg skips with "no Chromium" without both, while the rect predicates
that judge what a browser drew are pure and always run.
"""

from __future__ import annotations

import importlib
import itertools
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from eawf.observability.reflect.html import write_pages
from tests.unit.observability.reflect.test_html_pages import _corpus

WIDTHS = (900, 1280, 1600)
SCHEMES = ("light", "dark")

# scrollHeight may exceed clientHeight by this many px from rounding alone.
_SCROLL_SLACK = 1
# A text box this many px past its clipping box is cut off.
_CLIP_SLACK = 0.5
# Two boxes may share this many px² where antialiased edges touch.
_OVERLAP_SLACK = 1.0
_EPSILON = 1e-9


class Rect(BaseModel):
    """A client rect in CSS px."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: float
    top: float
    right: float
    bottom: float

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError(f"inverted rect {self}")
        return self

    def overhang(self, clip: Rect) -> float:
        """Return how far this box reaches past *clip* on its worst side; 0 inside it."""
        return max(
            clip.left - self.left,
            self.right - clip.right,
            clip.top - self.top,
            self.bottom - clip.bottom,
            0.0,
        )

    def overlap(self, other: Rect) -> float:
        """Return the area this box shares with *other*."""
        wide = min(self.right, other.right) - max(self.left, other.left)
        tall = min(self.bottom, other.bottom) - max(self.top, other.top)
        return max(wide, 0.0) * max(tall, 0.0)


class ScrollBox(BaseModel):
    """An element whose computed ``overflow-y`` is not ``visible``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    scroll_height: int
    client_height: int
    overflow_y: str


class TextBox(BaseModel):
    """One line box of text and the box of its nearest clipping ancestor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    rect: Rect
    clip: Rect


class ChartItem(BaseModel):
    """A text line or a mark inside one ``[data-chart]`` root.

    ``ident`` names the element it belongs to and ``within`` that element's
    ancestors, so a label is never judged against the mark that holds it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: int
    ident: int
    name: str
    rect: Rect
    within: frozenset[int]


class Drawn(BaseModel):
    """What the collector read from one drawn page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scroll: tuple[ScrollBox, ...]
    texts: tuple[TextBox, ...]
    items: tuple[ChartItem, ...]


def vertical_scrollers(boxes: Sequence[ScrollBox]) -> list[str]:
    """Return the boxes that scroll vertically: scrollable ``overflow-y`` and taller content."""
    return [
        f"{box.name} scrolls: {box.scroll_height} > {box.client_height} + {_SCROLL_SLACK}"
        for box in boxes
        if box.overflow_y in ("auto", "scroll")
        and box.scroll_height > box.client_height + _SCROLL_SLACK
    ]


def clipped_texts(boxes: Sequence[TextBox]) -> list[str]:
    """Return the text lines that reach at least :data:`_CLIP_SLACK` past their clip."""
    return [
        f"{box.name} text {box.rect.overhang(box.clip):.2f}px past its clip"
        for box in boxes
        if box.rect.overhang(box.clip) >= _CLIP_SLACK - _EPSILON
    ]


def overlapping(items: Sequence[ChartItem]) -> list[str]:
    """Return the pairs in one chart root, neither holding the other, sharing over 1px²."""
    found = []
    for first, second in itertools.combinations(items, 2):
        if first.root != second.root:
            continue
        if (
            first.ident == second.ident
            or first.ident in second.within
            or second.ident in first.within
        ):
            continue
        shared = first.rect.overlap(second.rect)
        if shared > _OVERLAP_SLACK + _EPSILON:
            found.append(f"{first.name} and {second.name} share {shared:.2f}px²")
    return found


def judge(drawn: Drawn) -> list[str]:
    """Return every geometry failure in one drawn page."""
    return [
        *vertical_scrollers(drawn.scroll),
        *clipped_texts(drawn.texts),
        *overlapping(drawn.items),
    ]


# ---- the predicates, on synthetic rect sets --------------------------------------------


def _rect(left: float, top: float, right: float, bottom: float) -> Rect:
    return Rect(left=left, top=top, right=right, bottom=bottom)


def _scroll(scroll: int, client: int, overflow: str = "auto") -> ScrollBox:
    return ScrollBox(name="div", scroll_height=scroll, client_height=client, overflow_y=overflow)


def _item(
    ident: int, rect: Rect, *, root: int = 0, within: frozenset[int] = frozenset()
) -> ChartItem:
    return ChartItem(root=root, ident=ident, name=f"n{ident}", rect=rect, within=within)


def test_rect_predicates() -> None:
    clip = _rect(10, 10, 110, 30)

    # Scrollers: one px of rounding passes, two fail; a hidden overflow never scrolls.
    assert vertical_scrollers([]) == []
    assert vertical_scrollers([_scroll(100, 100)]) == []
    assert vertical_scrollers([_scroll(101, 100)]) == []
    assert len(vertical_scrollers([_scroll(102, 100)])) == 1
    assert len(vertical_scrollers([_scroll(102, 100, "scroll")])) == 1
    assert vertical_scrollers([_scroll(500, 100, "hidden"), _scroll(500, 100, "clip")]) == []

    # Clipped text: 0.5px past any side fails; just under passes.
    assert clipped_texts([]) == []
    inside = TextBox(name="b", rect=_rect(10, 10, 110, 30), clip=clip)
    assert clipped_texts([inside]) == []
    assert clipped_texts([TextBox(name="b", rect=_rect(10, 10, 110.49, 30), clip=clip)]) == []
    for past in (
        _rect(9.5, 12, 50, 28),
        _rect(20, 12, 110.5, 28),
        _rect(20, 9.5, 50, 28),
        _rect(20, 12, 50, 30.5),
    ):
        assert len(clipped_texts([TextBox(name="b", rect=past, clip=clip)])) == 1, past
    assert _rect(0, 0, 5, 5).overhang(clip) == pytest.approx(10.0)

    # Overlap: exactly 1px² passes, more fails; touching edges share nothing.
    assert overlapping([]) == []
    assert overlapping([_item(1, _rect(0, 0, 10, 10))]) == []
    assert overlapping([_item(1, _rect(0, 0, 10, 10)), _item(2, _rect(9, 9, 20, 20))]) == []
    assert overlapping([_item(1, _rect(0, 0, 10, 10)), _item(2, _rect(10, 0, 20, 10))]) == []
    assert len(overlapping([_item(1, _rect(0, 0, 10, 10)), _item(2, _rect(9, 8.9, 20, 20))])) == 1
    big = [_item(1, _rect(0, 0, 10, 10)), _item(2, _rect(5, 5, 15, 15))]
    assert len(overlapping(big)) == 1
    # Other roots, the same element and a mark holding its label are never compared.
    assert overlapping([_item(1, _rect(0, 0, 10, 10)), _item(2, _rect(5, 5, 15, 15), root=1)]) == []
    assert overlapping([_item(1, _rect(0, 0, 10, 10)), _item(1, _rect(5, 5, 15, 15))]) == []
    held = _item(2, _rect(2, 2, 8, 8), within=frozenset({1, 2}))
    assert overlapping([_item(1, _rect(0, 0, 10, 10)), held]) == []
    assert _rect(0, 0, 10, 10).overlap(_rect(5, 5, 15, 15)) == pytest.approx(25.0)

    drawn = Drawn(scroll=(_scroll(102, 100),), texts=(inside,), items=tuple(big))
    assert len(judge(drawn)) == 2


def test_rect_refuses_an_inverted_box() -> None:
    with pytest.raises(ValidationError):
        _rect(10, 0, 9, 5)
    with pytest.raises(ValidationError):
        _rect(0, 5, 10, 4.9)
    with pytest.raises(ValidationError):
        Rect.model_validate({"left": 0, "top": 0, "right": 1, "bottom": 1, "width": 1})
    with pytest.raises(ValidationError):
        ScrollBox.model_validate({"name": "div", "scroll_height": "tall", "client_height": 1})


# ---- the browser leg -------------------------------------------------------------------

# Marks a chart draws; ribbons, grid lines and scatter dots overlap by design and are left out.
_MARKS = "svg rect, .tl-bar, .tl-mark b, .strip > button, .dist-label"

# Reads every scroller, every text line with its clip, and every chart text and mark.
# A scroller clips to its scrollable area, not its visible box, and a box styled to
# truncate with an ellipsis is cut on purpose, so neither counts as a clip.
_COLLECT = """
(marks) => {
  const ids = new Map();
  const id = (el) => { if (!ids.has(el)) ids.set(el, ids.size); return ids.get(el); };
  const box = (r) => ({left: r.left, top: r.top, right: r.right, bottom: r.bottom});
  const name = (el) => {
    const cls = (el.getAttribute('class') || '').trim();
    return el.tagName.toLowerCase() + (cls ? '.' + cls.split(/\\s+/).join('.') : '');
  };
  const lineage = (el) => {
    const out = [];
    for (; el; el = el.parentElement) out.push(id(el));
    return out;
  };
  const scrolls = (v) => v === 'auto' || v === 'scroll';
  const clipOf = (el) => {
    for (let at = el; at && at !== document.body; at = at.parentElement) {
      const s = getComputedStyle(at);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      if (s.textOverflow === 'ellipsis') return null;
      const r = at.getBoundingClientRect();
      if (scrolls(s.overflowX) || scrolls(s.overflowY)) {
        const left = r.left + at.clientLeft - at.scrollLeft;
        const top = r.top + at.clientTop - at.scrollTop;
        return {left, top, right: left + at.scrollWidth, bottom: top + at.scrollHeight};
      }
      return box(r);
    }
    return null;
  };
  const scroll = [];
  for (const el of document.body.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (s.overflowY !== 'visible') scroll.push({name: name(el), scroll_height: el.scrollHeight,
      client_height: el.clientHeight, overflow_y: s.overflowY});
  }
  const texts = [];
  const items = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.textContent.trim() || !el || el.closest('script, style, title')) continue;
    range.selectNodeContents(node);
    const clip = clipOf(el);
    const root = el.closest('[data-chart]');
    for (const r of range.getClientRects()) {
      if (!r.width || !r.height) continue;
      if (clip) texts.push({name: name(el), rect: box(r), clip});
      if (root) items.push({root: id(root), ident: id(el), name: name(el), rect: box(r),
        within: lineage(el)});
    }
  }
  for (const el of document.querySelectorAll(marks)) {
    const root = el.closest('[data-chart]');
    const r = el.getBoundingClientRect();
    if (!root || !r.width || !r.height) continue;
    items.push({root: id(root), ident: id(el), name: name(el), rect: box(r),
      within: lineage(el.parentElement)});
  }
  return {scroll, texts, items};
}
"""


def _browser() -> Any:
    """Return Playwright's sync API, or skip the browser leg when it is not installed."""
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        pytest.skip("no Chromium")


def _fixture_pages(root: Path) -> list[Path]:
    corpus = _corpus(45, flagged=range(1, 13))
    pages = write_pages(root, corpus.sources)
    session = pages.overview.parent / f"session-{corpus.detailed.removeprefix('vsid-')}.html"
    return [pages.overview, pages.finder, session]


def test_browser_geometry(tmp_path: Path) -> None:
    api = _browser()
    pages = _fixture_pages(tmp_path / "pages")
    failures: list[str] = []
    with api.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except api.Error:
            pytest.skip("no Chromium")
        try:
            for width, scheme in itertools.product(WIDTHS, SCHEMES):
                context = browser.new_context(
                    viewport={"width": width, "height": 900}, color_scheme=scheme
                )
                view = context.new_page()
                for page in pages:
                    view.goto(page.as_uri())
                    drawn = Drawn.model_validate(view.evaluate(_COLLECT, _MARKS))
                    failures += [f"{page.name} {width} {scheme}: {item}" for item in judge(drawn)]
                context.close()
        finally:
            browser.close()
    assert failures == []
