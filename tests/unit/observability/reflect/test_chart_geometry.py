"""Pure geometry checks on generated reflect pages: margins, end ticks, scrollers, lanes.

Every number is read from the generated page: positions from the markup and
sizes from its inlined stylesheet, so a drift in either reds here.

``tests/fixtures/reflect_geometry/session-0.7.1.html`` is the session page the
0.7.1 renderer drew for :func:`_rich_detail`, its script and its request,
finding and coverage sections cut; the page checks must all fail on it.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eawf.observability.reflect.generations import GenerationSummary
from eawf.observability.reflect.html import write_pages
from eawf.observability.reflect.html.session import (
    _Lane,
    _relax,
    _sankey,
    _timeline,
    _token_ticks,
    _totals,
)
from eawf.observability.reflect.html.shared import (
    axis_side,
    axis_ticks,
    compact,
    duration,
    label_width,
)
from eawf.observability.telemetry.models import DurationKind
from eawf.observability.telemetry.sources.session_history import (
    BlockKind,
    CompactionBoundary,
    SessionDetail,
    ToolSpan,
)
from tests.unit.observability.reflect import test_html_pages
from tests.unit.observability.reflect.test_html_pages import (
    _T0,
    _block,
    _corpus,
    _detail,
    _Node,
    _parse,
    _section,
    _Tree,
)

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SIDE = re.compile(r"--side:(\d+(?:\.\d+)?)px")
_LEFT = re.compile(r"left:(-?\d+(?:\.\d+)?)%")
_X = re.compile(r"--x:(-?\d+(?:\.\d+)?)%")
_WIDTHS = (300.0, 900.0, 1600.0)

Css = dict[str, dict[str, str]]


def _css(tree: _Tree) -> Css:
    """Return the page's inlined stylesheet as declarations by single selector."""
    style = next(node for node in tree.root.walk() if node.tag == "style").all_text()
    rules: Css = {}
    for selectors, body in _RULE.findall(_COMMENT.sub("", style)):
        decls = {
            name.strip(): value.strip()
            for name, _, value in (item.partition(":") for item in body.split(";"))
            if name.strip()
        }
        for selector in selectors.split(","):
            rules.setdefault(selector.strip(), {}).update(decls)
    return rules


def _px(value: str) -> float:
    assert value.endswith("px"), value
    return float(value.removesuffix("px"))


def _classes(node: _Node) -> list[str]:
    return node.attrs.get("class", "").split()


def _pages(tmp_path: Path) -> tuple[_Tree, _Tree]:
    corpus = _corpus(45, flagged=range(1, 13))
    pages = write_pages(tmp_path / "pages", corpus.sources)
    session = pages.overview.parent / f"session-{corpus.detailed.removeprefix('vsid-')}.html"
    return _parse(pages.overview), _parse(session)


def _box(at: float, label: str, font: float, anchor: float, width: float) -> tuple[float, float]:
    left = at / 100 * width - anchor * label_width(label, font)
    return left, left + label_width(label, font)


# ---- rules 1 and 2: fixed margins and end ticks ------------------------------------------


# (page, section, chart root class, axis class, axis selector, label selector, margin rules)
_CHARTS = (
    ("overview", "distribution", "dist", "axis", ".axis", ".axis b", (".dist",)),
    ("session", "timeline", "tl", "rl", ".tl-ruler .rl", ".tl-ruler .rl b", (".tl-track",)),
    ("session", "generations", "strip-in", "tokaxis", ".tokaxis", ".tokaxis b", (".strip-in",)),
)


def test_rules_1_2_margins_and_end_ticks(tmp_path: Path) -> None:
    trees = dict(zip(("overview", "session"), _pages(tmp_path), strict=True))
    checked = 0
    for page, name, root_cls, axis_cls, axis_sel, label_sel, margin_sels in _CHARTS:
        css = _css(trees[page])
        font = _px(css[label_sel]["font-size"])
        tick = max(
            _px(css[selector]["height"])
            for selector in (f"{axis_sel} i", f"{axis_sel} i.mj")
            if "height" in css.get(selector, {})
        )
        axis_rule = css[axis_sel]
        # Rule 1, bottom: the box is the tick plus one label line plus 4px.
        assert axis_rule["height"] == "auto"
        assert _px(axis_rule["padding-top"]) == pytest.approx(tick)
        assert _px(css[label_sel]["top"]) == pytest.approx(tick)
        assert float(css[label_sel]["line-height"]) == pytest.approx(1.2)
        assert _px(axis_rule["padding-top"]) + _px(axis_rule["padding-bottom"]) == pytest.approx(
            tick + 1.2 * font + 4
        )
        # Rule 2: the end labels anchor inward, inner ones centre.
        assert css[f"{label_sel}.t-first"]["transform"] == "translateX(0)"
        assert css[f"{label_sel}.t-last"]["transform"] == "translateX(-100%)"
        assert css[label_sel]["transform"] == "translateX(-50%)"
        for selector in margin_sels:
            assert "var(--side)" in " ".join(css[selector].values()), selector

        roots = _section(trees[page], name).find(cls=root_cls)
        assert roots, name
        for root in roots:
            match = _SIDE.search(root.attrs.get("style", ""))
            assert match, f"{name}: no fixed side margin"
            side = float(match.group(1))
            labels = [node for axis in root.find(cls=axis_cls) for node in axis.find(tag="b")]
            assert len(labels) >= 2, name
            texts = [node.all_text() for node in labels]
            # Rule 1, sides: at least half the widest label plus 4px.
            widest = max(label_width(text, font) for text in texts)
            assert side >= widest / 2 + 4
            assert _classes(labels[0]) == ["t-first"]
            assert _classes(labels[-1]) == ["t-last"]
            assert all(not _classes(node) for node in labels[1:-1])
            anchors = [0.0, *[0.5] * (len(labels) - 2), 1.0]
            for width in _WIDTHS:
                for node, text, anchor in zip(labels, texts, anchors, strict=True):
                    match = _LEFT.search(node.attrs["style"])
                    assert match
                    left, right = _box(float(match.group(1)), text, font, anchor, width)
                    assert left >= -side - 0.5
                    assert right <= width + side + 0.5
            checked += 1
    assert checked >= 3


def test_axis_ticks_boundaries() -> None:
    assert axis_ticks([]) == ""
    single = axis_ticks([(0.0, "0")])
    assert 'class="t-first"' in single
    assert "t-last" not in single
    pair = axis_ticks([(0.0, "0"), (100.0, "1k")])
    assert pair.index("t-first") < pair.index("t-last")
    three = axis_ticks([(0.0, "a"), (50.0, "<b>"), (100.0, "c")])
    assert three.count("<b ") + three.count("<b>") == 3
    assert "&lt;b&gt;" in three


def test_axis_ticks_refuses_a_non_numeric_offset() -> None:
    with pytest.raises(ValueError):
        axis_ticks([("left", "0")])  # type: ignore[list-item]


def test_axis_side_boundaries() -> None:
    assert axis_side([], 10.0) == 4
    assert axis_side([""], 10.0) == 4
    assert axis_side(["0"], 10.0) == 7
    assert axis_side(["0", "12:00:00"], 10.0) == 28
    assert label_width("compaction", 10.0) == pytest.approx(60.0)


def test_label_width_refuses_a_non_string() -> None:
    with pytest.raises(TypeError):
        label_width(5, 10.0)  # type: ignore[arg-type]


# ---- rules 3 and 5: scrollers -------------------------------------------------------------


def _view_box(node: _Node) -> tuple[float, float]:
    _x, _y, wide, tall = (float(part) for part in node.attrs["viewbox"].split())
    return wide, tall


def test_rules_3_5_scrollers(tmp_path: Path) -> None:
    overview, session = _pages(tmp_path)
    css = _css(session)
    scrollers = {
        selector.removeprefix(".")
        for selector, decls in css.items()
        if decls.get("overflow-x") == "auto"
    }
    assert {"strip-vp", "tbl-scroll", "wf-vp"} <= scrollers
    # Rule 3: a horizontal scroller never scrolls vertically.
    for selector, decls in css.items():
        if decls.get("overflow-x") == "auto":
            assert decls.get("overflow-y") == "hidden", selector
    assert css == _css(overview)

    # Rule 3: the token axis is as tall as its labels.
    axis = css[".tokaxis"]
    assert axis["height"] == "auto"
    assert _px(axis["padding-bottom"]) == pytest.approx(16.0)
    label = css[".tokaxis b"]
    label_bottom = _px(label["top"]) + float(label["line-height"]) * _px(label["font-size"])
    assert _px(axis["padding-top"]) + _px(axis["padding-bottom"]) >= label_bottom + 4

    generations = _section(session, "generations")
    viewports = generations.find(cls="strip-vp")
    assert viewports
    assert all(viewport.find(cls="tokaxis") for viewport in viewports)

    # Rule 5: an SVG in a horizontal scroller draws at its viewBox size.
    assert "width" not in css.get(".wf svg", {})
    svgs = _section(session, "compaction").find(tag="svg")
    assert svgs
    inside = [
        svg
        for node in _section(session, "compaction").walk()
        if set(_classes(node)) & scrollers
        for svg in node.find(tag="svg")
    ]
    assert len(inside) == len(svgs)
    for svg in inside:
        wide, tall = _view_box(svg)
        assert float(svg.attrs["width"]) == pytest.approx(wide)
        assert float(svg.attrs["height"]) == pytest.approx(tall)


# ---- rules 6 and 7: compaction markers and distribution lanes ----------------------------


def _marks(at: float) -> _Node:
    """Return the timeline of the fixture session with one compaction at *at* seconds."""
    detail = _detail(0, _T0)
    cut = detail.compactions[0].model_copy(update={"ts": _T0 + timedelta(seconds=at)})
    assert isinstance(cut, CompactionBoundary)
    tree = _Tree()
    tree.feed(_timeline(detail.model_copy(update={"compactions": (cut,)})))
    tree.close()
    return tree.root


def test_rules_6_7_markers_and_lanes(tmp_path: Path) -> None:
    overview, session = _pages(tmp_path)
    css = _css(session)

    # Rule 6: an 18px marker row above the bars, chips on the panel colour.
    base = _px(css[".tl-track"]["height"])
    assert _px(css[".tl-track.marked"]["height"]) == pytest.approx(base + 18)
    assert _px(css[".tl-track.marked .tl-bar"]["top"]) >= 18
    assert _px(css[".tl-track.marked .ph-idle"]["top"]) >= 18
    chip = css[".tl-mark b"]
    assert _px(chip["top"]) + _px(chip["height"]) <= 18
    assert chip["background"] == "var(--panel)"
    assert chip["--chip"] == f"calc({len('compaction')}ch + 14px)"
    assert chip["left"].startswith("clamp(0px,")
    assert chip["left"].endswith(",calc(100% - var(--chip)))")
    flipped = css[".tl-mark.flip b"]["left"]
    assert flipped.startswith("clamp(0px,calc(var(--x) - var(--chip)")
    assert flipped.endswith(",calc(100% - var(--chip)))")

    for at, flip in ((5.0, False), (190.0, False), (359.0, True), (360.0, True)):
        root = _marks(at)
        tracks = root.find(cls="tl-track")
        marked = [track for track in tracks if "marked" in _classes(track)]
        assert len(marked) == 1
        marks = marked[0].find(cls="tl-mark")
        assert len(marks) == 1
        match = _X.search(marks[0].attrs["style"])
        assert match
        x = float(match.group(1))
        assert 0.0 <= x <= 100.0
        assert (x > 85) is flip
        assert ("flip" in _classes(marks[0])) is flip
        assert marks[0].find(tag="b")[0].all_text() == "compaction"
    page_marks = _section(session, "timeline").find(cls="tl-mark")
    assert page_marks
    assert all(
        "marked" in _classes(track)
        for track in _section(session, "timeline").find(cls="tl-track")
        if track.find(cls="tl-mark")
    )

    # Rule 7: distribution labels sit in a lane above the dots.
    css = _css(overview)
    assert css[".dist"]["padding-top"] == "3em"
    em = _px(css["body"]["font"].split("/")[0])
    lane_height = 3 * em
    dist = _section(overview, "distribution").find(cls="dist")[0]
    lanes = dist.find(cls="dist-lane")
    assert len(lanes) == 1
    labels = lanes[0].find(cls="dist-label")
    rows = ("p50", "p95", "max")
    assert {name for node in labels for name in rows if name in _classes(node)} == set(rows)
    track = dist.find(cls="dist-track")[0]
    assert not track.find(cls="dist-label")
    assert track.find(cls="dist-dot")
    font = _px(css[".dist-label"]["font-size"])
    line = _px(css[".dist-label"]["line-height"])
    boxes = []
    for node in labels:
        row = next(name for name in ("p50", "p95", "max") if name in _classes(node))
        top = _px(css[f".dist-label.{row}"]["top"])
        assert top + line <= lane_height
        match = _LEFT.search(node.attrs["style"])
        assert match
        anchor = 1.0 if "flip" in _classes(node) else 0.0
        boxes.append((float(match.group(1)), node.all_text(), anchor, top))
    for width in _WIDTHS:
        rects = [
            (*_box(at, text, font, anchor, width), top, top + line)
            for at, text, anchor, top in boxes
        ]
        for first, second in itertools.combinations(rects, 2):
            overlap_x = min(first[1], second[1]) - max(first[0], second[0])
            overlap_y = min(first[3], second[3]) - max(first[2], second[2])
            assert max(overlap_x, 0) * max(overlap_y, 0) <= 1.0


# ---- rules 4 and 8: Sankey labels, kind colours, formats and totals ----------------------

_FIXTURE_071 = Path(__file__).parents[3] / "fixtures" / "reflect_geometry" / "session-0.7.1.html"
_PCT = re.compile(r"(\d+(?:\.\d+)?)% of generation")
# A compact count: digits, an optional decimal and a unit letter, alone as a word.
_COUNT = re.compile(r"(?<![\w.:])(\d+(?:\.\d+)?)([kmMbB])(?![\w])")
_HOURS_MINUTES = re.compile(r"\b\d+h \d{2}m\b")
_DECIMAL_SPAN = re.compile(r"(?<![\w.])\d+\.\d+[hd]\b")
_RULER_LABEL = re.compile(r"^\d+(s|min|h|d)$")
_TOKEN_LABEL = re.compile(r"^\d+(?:\.\d)?([kMB]?)$")

Problems = list[str]


def _rich_detail(index: int, start: datetime) -> SessionDetail:
    """The fixture session with every context kind, four of them under 2% of generation 1."""
    base = _ORIGINAL_DETAIL(index, start)
    kinds: list[tuple[BlockKind, int, int]] = [
        ("system", 0, 12_000),
        ("tool_definition", 0, 9_000),
        ("project_instruction", 0, 3_000),
        ("runtime_injection", 0, 600),
        ("user", 0, 400),
    ]
    for request in range(1, 7):
        kinds += [("assistant", request, 200), ("tool_call", request, 120)]
        kinds += [("tool_result", request, 8_000)]
    kinds += [("assistant_reasoning", 6, 500)]
    cut = len(kinds)
    kinds += [("compaction_summary", 6, 4_000), ("user", 6, 300)]
    for request in range(7, 13):
        kinds += [("assistant", request, 200), ("tool_result", request, 3_000)]
    blocks = tuple(
        _block(ordinal, kind, request, start, chars)
        for ordinal, (kind, request, chars) in enumerate(kinds, start=1)
    )
    cutoff = base.compactions[0].model_copy(update={"block_ordinal": cut})
    return base.model_copy(update={"blocks": blocks, "compactions": (cutoff,)})


_ORIGINAL_DETAIL = _detail


def _rich_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Tree:
    monkeypatch.setattr(test_html_pages, "_detail", _rich_detail)
    corpus = _corpus(3)
    pages = write_pages(tmp_path / "pages", corpus.sources)
    return _parse(pages.overview.parent / f"session-{corpus.detailed.removeprefix('vsid-')}.html")


def _sankey_font(css: Css) -> float:
    return _px(css[".wf svg text"]["font"].split()[0])


def _texts(node: _Node) -> Iterator[str]:
    """Yield every text a reader can see or hover on, outside the stylesheet and script."""
    if node.tag in ("style", "script"):
        return
    yield from node.text
    if "title" in node.attrs:
        yield node.attrs["title"]
    for child in node.children:
        yield from _texts(child)


def _u1_scrollers(tree: _Tree) -> Problems:
    css = _css(tree)
    problems = [
        f"{selector}: overflow-x:auto beside overflow-y:{decls.get('overflow-y', 'visible')}"
        for selector, decls in css.items()
        if decls.get("overflow-x") == "auto" and decls.get("overflow-y") != "hidden"
    ]
    if css.get(".tokaxis", {}).get("height") != "auto":
        problems.append(".tokaxis: a fixed height clips its labels")
    return problems


def _u2_end_ticks(tree: _Tree) -> Problems:
    problems = []
    for cls in ("rl", "tokaxis", "axis"):
        for axis in tree.root.find(cls=cls):
            labels = axis.find(tag="b")
            if len(labels) < 2:
                continue
            if _classes(labels[0]) != ["t-first"] or _classes(labels[-1]) != ["t-last"]:
                problems.append(f"{cls}: end labels not anchored inward")
    return problems


def _u4_sankey(tree: _Tree) -> Problems:
    """Rule 4 on every Sankey: order, spacing, leaders, folding, viewBox and gutter."""
    font = _sankey_font(_css(tree))
    problems: Problems = []
    for svg in _section(tree, "compaction").find(tag="svg"):
        rects = [node for node in svg.find(tag="rect") if _classes(node)[0].startswith("k-")]
        left = min(float(node.attrs["x"]) for node in rects)
        nodes = sorted(
            (node for node in rects if float(node.attrs["x"]) == left),
            key=lambda node: float(node.attrs["y"]),
        )
        labels = [
            node
            for node in svg.find(tag="text")
            if node.attrs.get("text-anchor") == "end" and float(node.attrs["x"]) < left
        ]
        ys = [float(node.attrs["y"]) for node in labels]
        if ys != sorted(ys):
            problems.append("labels not sorted by y")
        problems += [
            f"labels {first:.1f} and {second:.1f} closer than 1.2 x font-size"
            for first, second in itertools.pairwise(ys)
            if second - first < 1.2 * font - 0.05
        ]
        centres = [float(node.attrs["y"]) + float(node.attrs["height"]) / 2 for node in nodes]
        moved = [
            centre for centre, y in zip(centres, sorted(ys), strict=False) if y - (centre + 4) > 0.5
        ]
        leaders = [float(node.attrs["y2"]) for node in svg.find(cls="lead")]
        if len(leaders) != len(moved) or any(
            # Printed to 0.1px, and a sliver node is drawn 0.5px tall around its true centre.
            abs(end - centre) > 0.3
            for end, centre in zip(leaders, moved, strict=False)
        ):
            problems.append(f"{len(moved)} moved labels, {len(leaders)} leader lines")
        for node, label in zip(
            nodes, sorted(labels, key=lambda n: float(n.attrs["y"])), strict=False
        ):
            share = _PCT.search(node.all_text())
            if share and float(share.group(1)) < 2 and not label.all_text().startswith("Other ("):
                problems.append(f"{label.all_text()}: under 2% and not folded")
        _x, _y, _wide, tall = (float(part) for part in svg.attrs["viewbox"].split())
        if ys and tall < ys[-1] + 4:
            problems.append(f"viewBox height {tall} stops above the last label at {ys[-1]}")
        widest = max(label_width(node.all_text(), font) for node in labels)
        if abs(left - (widest + 12)) > 0.06:
            problems.append(f"gutter {left} is not the widest label {widest:.1f} + 12")
    return problems


def _u5_svg_size(tree: _Tree) -> Problems:
    problems = []
    for svg in _section(tree, "compaction").find(tag="svg"):
        _x, _y, wide, tall = (float(part) for part in svg.attrs["viewbox"].split())
        drawn = (svg.attrs.get("width"), svg.attrs.get("height"))
        if drawn != (f"{wide:.0f}", f"{tall:.0f}"):
            problems.append(f"svg drawn at {drawn}, viewBox {wide:.0f}x{tall:.0f}")
    return problems


def _u10_kind_colours(tree: _Tree) -> Problems:
    """One class per kind word, one colour per class."""
    words: dict[str, set[str]] = {}
    for item in _section(tree, "generations").find(cls="strip-legend"):
        for span in item.find(tag="span"):
            swatch = span.find(tag="i")[0]
            words.setdefault(_classes(swatch)[0], set()).add(span.all_text().rsplit(" ", 1)[0])
    problems = [
        f"{cls} drawn for {sorted(names)}" for cls, names in words.items() if len(names) > 1
    ]
    css = _css(tree)
    colours: dict[str, str] = {}
    for cls in words:
        colour = css[f".{cls}"]["background"]
        if colour in colours.values():
            problems.append(f"{cls} shares its colour {colour}")
        colours[cls] = colour
    return problems


def _u11_formats(tree: _Tree) -> Problems:
    """Rule 8 formats: compact counts, durations and one unit per axis."""
    problems = []
    for text in _texts(tree.root):
        for count, unit in _COUNT.findall(_HOURS_MINUTES.sub("", text)):
            if unit in "mb" or count.endswith(".0"):
                problems.append(f"count {count}{unit} in {text!r}")
        problems += [f"span {span} in {text!r}" for span in _DECIMAL_SPAN.findall(text)]
    for cls, pattern in (("rl", _RULER_LABEL), ("tokaxis", _TOKEN_LABEL)):
        for axis in tree.root.find(cls=cls):
            labels = [node.all_text() for node in axis.find(tag="b")]
            units = {match.group(1) if match else None for match in map(pattern.match, labels)}
            if None in units or len(units) > 1:
                problems.append(f"{cls} labels {labels} not in one unit")
    return problems


_CHECKS: dict[str, Callable[[_Tree], Problems]] = {
    "U1": _u1_scrollers,
    "U2": _u2_end_ticks,
    "U4": _u4_sankey,
    "U5": _u5_svg_size,
    "U10": _u10_kind_colours,
    "U11": _u11_formats,
}


def _summary(index: int, tokens: dict[str, int]) -> GenerationSummary:
    return GenerationSummary(
        index=index,
        reason="synthetic",
        requests=1,
        appended_tokens=sum(tokens.values()),
        tokens_by_kind=tokens,
        final_request_input=None,
        reconciliation_delta=None,
    )


def _svg_tree(markup: str) -> _Tree:
    tree = _Tree()
    tree.feed(
        f'<style>.wf svg text{{font:10.5px var(--mono)}}</style><section data-section="compaction">'
        f"{markup}</section>"
    )
    tree.close()
    return tree


def test_rule_4_sankey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _rich_session(tmp_path, monkeypatch)
    assert _u4_sankey(session) == []
    svg = _section(session, "compaction").find(tag="svg")[0]
    texts = [node.all_text() for node in svg.find(tag="text")]
    # Model replies, tool calls, runtime injections and your messages: 730 of 18,730 tokens.
    assert "Other (4) 730" in texts
    assert not any(text.startswith(("Your messages", "Runtime injections")) for text in texts)
    assert svg.find(cls="lead")

    # Seven close kinds and four tiny ones: the labels relax down past the nodes,
    # each moved one gets a leader, and the viewBox grows to the last.
    crowded = {
        "tool_result": 1_000_000,
        **dict.fromkeys(("system", "user", "assistant", "tool_call", "tool_definition"), 30000),
        "project_instruction": 25_000,
        **dict.fromkeys(("runtime_injection", "assistant_reasoning", "unknown"), 5000),
    }
    for after in ({"compaction_summary": 40_000}, {}):
        tree = _svg_tree(_sankey(_summary(1, crowded), _summary(2, after), None))
        assert _u4_sankey(tree) == []
        labels = [node.all_text() for node in tree.root.find(tag="text")]
        assert "Other (3) 15k" in labels
        assert "Project instructions 25k" in labels
        assert len(tree.root.find(cls="lead")) >= 5
    # A single kind keeps its label at its node and draws no leader.
    alone = _svg_tree(_sankey(_summary(1, {"tool_result": 900}), _summary(2, {}), None))
    assert _u4_sankey(alone) == []
    assert not alone.root.find(cls="lead")
    gutter = float(alone.root.find(tag="rect")[0].attrs["x"])
    assert gutter == pytest.approx(label_width("Tool results 900", 10.5) + 12)
    # Exactly 2% stays its own row; just under folds.
    edge = _svg_tree(
        _sankey(_summary(1, {"tool_result": 98_000, "user": 2_000}), _summary(2, {}), None)
    )
    assert "Your messages 2k" in [node.all_text() for node in edge.root.find(tag="text")]
    under = _svg_tree(
        _sankey(_summary(1, {"tool_result": 98_001, "user": 1_999}), _summary(2, {}), None)
    )
    assert "Other (1) 2k" in [node.all_text() for node in under.root.find(tag="text")]


def test_rule_4_check_reds_on_crowded_labels() -> None:
    markup = (
        '<svg viewBox="0 0 400 14" width="400" height="14">'
        '<rect class="k-tool" x="100.0" y="0" width="12" height="5"><title>a, 50.0% of generation 1'
        '</title></rect><rect class="k-sys" x="100.0" y="6" width="12" height="5"><title>b, 50.0% '
        'of generation 1</title></rect><text x="94" y="6.5" text-anchor="end">Tool results 5</text>'
        '<text x="94" y="12.5" text-anchor="end">System prompt 5</text></svg>'
    )
    problems = _u4_sankey(_svg_tree(markup))
    assert any("closer than" in problem for problem in problems)
    assert any("gutter" in problem for problem in problems)
    assert any("viewBox height" in problem for problem in problems)


def test_relax_boundaries() -> None:
    assert _relax([], 12.6, 10.5) == []
    assert _relax([4.0], 12.6, 10.5) == [10.5]
    assert _relax([20.0, 32.6], 12.6, 10.5) == pytest.approx([20.0, 32.6])
    assert _relax([20.0, 32.5], 12.6, 10.5) == pytest.approx([20.0, 32.6])
    assert _relax([20.0, 20.0, 20.0], 12.6, 10.5) == pytest.approx([20.0, 32.6, 45.2])


def test_relax_refuses_a_non_numeric_baseline() -> None:
    with pytest.raises(TypeError):
        _relax(["20"], 12.6, 10.5)  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (950, "950"),
        (999, "999"),
        (999.6, "1k"),
        (1_000, "1k"),
        (50_000, "50k"),
        (99_960, "100k"),
        (123_456, "123k"),
        (999_950, "1M"),
        (1_300_000, "1.3M"),
        (10_000_000_000, "10B"),
        (2_500_000_000_000, "2500B"),
        (-50_000, "-50k"),
        (-0.4, "0"),
    ],
)
def test_compact_boundaries(value: float, text: str) -> None:
    assert compact(value) == text


@pytest.mark.parametrize(
    ("milliseconds", "text"),
    [
        (None, chr(0x2013)),
        (0, "0s"),
        (45_000, "45s"),
        (59_400, "59s"),
        (59_600, "1min"),
        (12 * 60_000, "12min"),
        (59 * 60_000 + 29_000, "59min"),
        (59 * 60_000 + 31_000, "1h 00m"),
        ((3 * 60 + 5) * 60_000, "3h 05m"),
        ((23 * 60 + 59) * 60_000 + 31_000, "1d 0h"),
        ((2 * 24 + 4) * 3_600_000, "2d 4h"),
    ],
)
def test_duration_boundaries(milliseconds: float | None, text: str) -> None:
    assert duration(milliseconds) == text


@pytest.mark.parametrize("function", [compact, duration])
def test_formats_refuse_text(function: Callable[[str], str]) -> None:
    with pytest.raises(TypeError):
        function("12")


def _lane(*spans: tuple[float, float, str]) -> _Lane:
    return _Lane(
        "lane",
        [(_T0 + timedelta(seconds=a), _T0 + timedelta(seconds=b), name) for a, b, name in spans],
    )


def test_totals_sum_real_intervals() -> None:
    empty, idle_busy = _totals([_lane()], 100.0)
    assert empty == pytest.approx({"idle": 100.0})
    assert idle_busy == pytest.approx(0.0)
    totals, busy = _totals(
        [
            _lane((0, 10, "write"), (5, 15, "write"), (20, 21, "tool"), (12, 12, "tool")),
            _lane((0, 30, "agent"), (10, 40, "agent")),
            _lane((50, 60, "agent")),
        ],
        100.0,
    )
    assert totals == pytest.approx({"write": 15.0, "tool": 1.0, "agent": 50.0, "idle": 84.0})
    assert busy == pytest.approx(16.0)
    full, busy = _totals([_lane((0, 100, "think"), (40, 50, "tool"))], 100.0)
    assert full == pytest.approx({"think": 100.0, "tool": 10.0})
    assert busy == pytest.approx(100.0)


def test_token_ticks_one_unit() -> None:
    assert [label for _at, label in _token_ticks(1)] == ["0", "1"]
    assert [label for _at, label in _token_ticks(800)] == [
        "0", "100", "200", "300", "400", "500", "600", "700", "800"
    ]  # fmt: skip
    assert [label for _at, label in _token_ticks(1_500)] == [
        "0k", "0.2k", "0.4k", "0.6k", "0.8k", "1k", "1.2k", "1.4k"
    ]  # fmt: skip
    assert [label for _at, label in _token_ticks(3_500_000)][-2:] == ["3M", "3.5M"]


def test_rule_8_formats_and_fire_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Numbers and durations.
    assert [compact(value) for value in (950, 50_000, 1_300_000, 10_000_000_000)] == [
        "950", "50k", "1.3M", "10B"
    ]  # fmt: skip
    assert [
        duration(ms) for ms in (45_000, 12 * 60_000, (3 * 60 + 5) * 60_000, 52 * 3_600_000)
    ] == ["45s", "12min", "3h 05m", "2d 4h"]

    # One colour per kind: the four kinds that shared two classes now have one each.
    session = _rich_session(tmp_path, monkeypatch)
    legend = {
        span.all_text().rsplit(" ", 1)[0]: _classes(span.find(tag="i")[0])[0]
        for item in _section(session, "generations").find(cls="strip-legend")
        for span in item.find(tag="span")
    }
    assert legend["System prompt"] == "k-sys"
    assert legend["Runtime injections"] == "k-inj"
    assert legend["Project instructions"] == "k-proj"
    assert legend["Tool results"] == "k-tool"
    assert len(set(legend.values())) == len(legend)

    # One ruler unit per axis: the fixture's 330 s window rules in whole minutes.
    ruler = [
        node.all_text() for node in _section(session, "timeline").find(cls="rl")[0].find(tag="b")
    ]
    assert ruler == ["0min", "1min", "2min", "3min", "4min", "5min"]

    # Timeline totals sum real intervals: one 1s tool call reads 1s, not a slot.
    base = _detail(0, _T0)
    one = ToolSpan(
        label="Read",
        started_at=_T0 + timedelta(seconds=100),
        ended_at=_T0 + timedelta(seconds=101),
        duration_ms=1_000,
    )
    spans = {**base.tool_spans, DurationKind.TOOL_CALL: (one,)}
    markup = _timeline(base.model_copy(update={"tool_spans": spans}))
    assert '<i class="ph-tool"></i>Tool call 1s</span>' in markup

    # The fire proof: every check passes on this page and fails on the 0.7.1 one.
    old = _parse(_FIXTURE_071)
    verdicts = {name: (check(session), check(old)) for name, check in _CHECKS.items()}
    assert {name: now for name, (now, _then) in verdicts.items()} == {name: [] for name in _CHECKS}
    assert [name for name, (_now, then) in verdicts.items() if not then] == []
