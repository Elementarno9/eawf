"""Unit tests for ``tools/html_probe.py``.

The probe answers a layout question about a rendered page -- what geometry
does it declare, and do any two declared rectangles collide -- without a
browser, so a render can be compared against a mockup in CI and in a
worktree. These tests pin the report over a fixture page plus the
boundaries the parser has to survive: an empty page, one element, a
self-closing void tag, unbalanced end tags, and edge-adjacent boxes that
must NOT count as overlapping.

The tool module is loaded via :mod:`importlib` because ``tools/`` is
excluded from the package and so is not importable by name.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "html_probe.py"

#: The fixture page. A header strip above a sidebar and a body pane that sit
#: edge to edge, plus a nested badge inside the body and a geometry-free
#: paragraph that must not appear as a box.
FIXTURE_PAGE = """<!doctype html>
<html>
  <body>
    <div id="header" class="chrome bar" style="left:0; top:0; width:1200px; height:40px"></div>
    <div id="sidebar" class="pane" style="left: 0; top: 40px; width: 240px; height: 760px">
      <p>navigation</p>
    </div>
    <div id="body" class="pane" style="left:240px; top:40px; width:960px; height:760px">
      <span id="badge" style="left:1150px; top:48px; width:40px; height:16px"></span>
      <img src="logo.png" width="32" height="32">
    </div>
  </body>
</html>
"""


def _load_module() -> Any:
    tool_dir = str(_TOOL_PATH.parent)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    spec = importlib.util.spec_from_file_location("html_probe", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["html_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe() -> Any:
    return _load_module()


@pytest.fixture()
def fixture_page(tmp_path: Path) -> Path:
    page = tmp_path / "fixture_page.html"
    page.write_text(FIXTURE_PAGE, encoding="utf-8")
    return page


# --------------------------------------------------------------------------- #
# The geometry report over the fixture page.
# --------------------------------------------------------------------------- #


def test_probe_file_returns_a_geometry_report_over_the_fixture_page(
    probe: Any, fixture_page: Path
) -> None:
    """The fixture page yields one box per geometry-declaring element."""
    report = probe.probe_file(fixture_page)

    assert report.source == str(fixture_page)
    assert [box.identifier for box in report.boxes] == ["header", "sidebar", "body", "badge", None]
    assert report.element_count == 8


def test_report_reads_inline_style_lengths_as_pixels(probe: Any) -> None:
    """An inline ``style`` length lands on the box as a float pixel count."""
    sidebar = probe.probe_html(FIXTURE_PAGE).boxes[1]

    assert (sidebar.left, sidebar.top) == (0.0, 40.0)
    assert (sidebar.width, sidebar.height) == (240.0, 760.0)
    assert sidebar.label == "div#sidebar.pane"


def test_report_reads_presentation_attributes_when_no_inline_style(probe: Any) -> None:
    """``width``/``height`` attributes are read when the element has no style."""
    logo = probe.probe_html(FIXTURE_PAGE).boxes[-1]

    assert (logo.tag, logo.width, logo.height) == ("img", 32.0, 32.0)
    assert logo.is_positioned is False


def test_report_records_nesting_depth(probe: Any) -> None:
    """A nested element carries a deeper ``depth`` than its parent."""
    boxes = {box.identifier: box for box in probe.probe_html(FIXTURE_PAGE).boxes}

    assert boxes["body"].depth == 2
    assert boxes["badge"].depth == boxes["body"].depth + 1


def test_geometry_free_elements_contribute_no_box(probe: Any) -> None:
    """An element declaring no geometry is counted but never reported."""
    report = probe.probe_html("<div><p>prose</p></div>")

    assert report.element_count == 2
    assert report.boxes == ()


def test_overlapping_pairs_reports_the_nested_and_colliding_boxes(probe: Any) -> None:
    """The badge collides with the body it sits in, and with nothing else."""
    report = probe.probe_html(FIXTURE_PAGE)

    labels = {(first.identifier, second.identifier) for first, second in report.overlapping_pairs()}

    assert labels == {("body", "badge")}


def test_edge_adjacent_boxes_do_not_overlap(probe: Any) -> None:
    """Two panes sharing a boundary are laid out, not colliding (off-by-one)."""
    markup = (
        '<div id="a" style="left:0;top:0;width:100px;height:10px"></div>'
        '<div id="b" style="left:100px;top:0;width:100px;height:10px"></div>'
    )

    assert probe.probe_html(markup).overlapping_pairs() == ()


def test_one_pixel_of_shared_area_does_overlap(probe: Any) -> None:
    """Shrinking the gap by one pixel flips the same pair to overlapping."""
    markup = (
        '<div id="a" style="left:0;top:0;width:100px;height:10px"></div>'
        '<div id="b" style="left:99px;top:0;width:100px;height:10px"></div>'
    )

    assert len(probe.probe_html(markup).overlapping_pairs()) == 1


# --------------------------------------------------------------------------- #
# Boundaries.
# --------------------------------------------------------------------------- #


def test_empty_page_yields_an_empty_report(probe: Any) -> None:
    """An empty page is a finding (no geometry), never an error."""
    report = probe.probe_html("")

    assert (report.element_count, report.boxes) == (0, ())
    assert "no element declares" in report.render()


def test_single_element_page_yields_one_box_at_depth_zero(probe: Any) -> None:
    """The one-element boundary reports depth 0 and order 0."""
    box = probe.probe_html('<div style="width:1px"></div>').boxes[0]

    assert (box.depth, box.order, box.width) == (0, 0, 1.0)


def test_void_tag_does_not_deepen_the_following_sibling(probe: Any) -> None:
    """An unclosed void tag must not leave the depth counter incremented."""
    markup = '<br><div id="after" style="width:2px"></div>'

    assert probe.probe_html(markup).boxes[0].depth == 0


def test_self_closing_tag_does_not_deepen_the_following_sibling(probe: Any) -> None:
    """An XHTML-style self-closing tag leaves the depth counter alone."""
    markup = '<img src="x.png"/><div id="after" style="width:2px"></div>'

    assert probe.probe_html(markup).boxes[0].depth == 0


def test_unbalanced_end_tag_clamps_depth_at_the_root(probe: Any) -> None:
    """Stray closing tags cannot drive the depth counter negative."""
    markup = '</div></div><div id="after" style="width:2px"></div>'

    assert probe.probe_html(markup).boxes[0].depth == 0


def test_zero_length_is_kept_not_treated_as_absent(probe: Any) -> None:
    """``0`` is a declared length; only a missing property yields ``None``."""
    box = probe.probe_html('<div style="left:0px;top:0;width:0;height:0"></div>').boxes[0]

    assert (box.left, box.top, box.width, box.height) == (0.0, 0.0, 0.0, 0.0)
    assert box.is_positioned is True


def test_parse_style_skips_malformed_and_trailing_segments(probe: Any) -> None:
    """A stray ``;`` or a colon-free segment costs nothing."""
    assert probe.parse_style("width:1px;;garbage;height: 2px;") == {
        "width": "1px",
        "height": "2px",
    }


def test_parse_length_accepts_px_and_unitless(probe: Any) -> None:
    """Both supported length forms resolve to the same pixel count."""
    assert probe.parse_length(" 240PX ") == pytest.approx(240.0)
    assert probe.parse_length("240") == pytest.approx(240.0)


# --------------------------------------------------------------------------- #
# Error paths.
# --------------------------------------------------------------------------- #


def test_parse_length_rejects_a_non_string(probe: Any) -> None:
    """A non-string length raises ``TypeError`` naming the type."""
    with pytest.raises(TypeError, match="must be a string"):
        probe.parse_length(240)


def test_parse_length_rejects_an_empty_length(probe: Any) -> None:
    """An empty length raises ``ValueError`` rather than defaulting to zero."""
    with pytest.raises(ValueError, match="empty"):
        probe.parse_length("   ")


@pytest.mark.parametrize("value", ["50%", "3em", "auto", "inherit"])
def test_parse_length_rejects_an_unresolvable_unit(probe: Any, value: str) -> None:
    """A unit needing computed layout raises instead of being coerced."""
    with pytest.raises(ValueError, match="unsupported length"):
        probe.parse_length(value)


def test_probe_html_propagates_an_unresolvable_declared_length(probe: Any) -> None:
    """A page declaring a percentage fails loudly, not silently box-less."""
    with pytest.raises(ValueError, match="unsupported length"):
        probe.probe_html('<div style="width:50%"></div>')


def test_probe_html_rejects_non_string_markup(probe: Any) -> None:
    """``probe_html`` fails fast on a non-string page."""
    with pytest.raises(TypeError, match="markup must be a string"):
        probe.probe_html(b"<div></div>")


def test_probe_html_rejects_non_string_source_label(probe: Any) -> None:
    """``probe_html`` fails fast on a non-string source label."""
    with pytest.raises(TypeError, match="source must be a string"):
        probe.probe_html("<div></div>", source=Path("page.html"))


def test_parse_style_rejects_non_string_input(probe: Any) -> None:
    """``parse_style`` fails fast on a non-string attribute value."""
    with pytest.raises(TypeError, match="style must be a string"):
        probe.parse_style(None)


def test_rectangle_raises_for_a_partially_declared_box(probe: Any) -> None:
    """A width-only element has no rectangle, and says so."""
    box = probe.probe_html('<div id="w" style="width:10px"></div>').boxes[0]

    with pytest.raises(ValueError, match="no complete rectangle"):
        box.rectangle()


def test_probe_file_raises_for_a_missing_page(probe: Any, tmp_path: Path) -> None:
    """A missing page raises ``FileNotFoundError``, never an empty report."""
    with pytest.raises(FileNotFoundError):
        probe.probe_file(tmp_path / "absent.html")


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def test_main_prints_the_geometry_table_and_exits_zero(
    probe: Any, fixture_page: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default CLI run renders the table and exits 0."""
    code = probe.main([str(fixture_page)])
    out = capsys.readouterr().out

    assert code == 0
    assert "geometry report:" in out
    assert "div#sidebar.pane" in out
    assert "overlap: div#body.pane x span#badge" in out


def test_main_json_mode_emits_the_serialisable_report(
    probe: Any, fixture_page: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` emits a parseable report carrying every box row."""
    code = probe.main([str(fixture_page), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["source"] == str(fixture_page)
    assert len(payload["boxes"]) == 5
    assert payload["overlaps"] == [["div#body.pane", "span#badge"]]


def test_main_returns_two_for_a_missing_page(
    probe: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing page exits 2 with the reason on stderr."""
    code = probe.main([str(tmp_path / "absent.html")])

    assert code == 2
    assert "html_probe:" in capsys.readouterr().err


def test_main_returns_two_for_an_unresolvable_length(
    probe: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page the probe cannot resolve exits 2 rather than raising."""
    page = tmp_path / "percent.html"
    page.write_text('<div style="height:50%"></div>', encoding="utf-8")

    code = probe.main([str(page)])

    assert code == 2
    assert "unsupported length" in capsys.readouterr().err
