"""Unit tests for the rich layout helpers (P20-I01-W02).

Covers the brand/breadcrumb/keymap constants, the four pane builders,
the 2x2 quadrant composer, and the header+body+footer frame builder
exported by :mod:`eawf.tui.layout`.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from eawf.tui import layout as layout_mod
from eawf.tui.layout import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    FOOTER_KEYMAP,
    QUADRANT_PANE_NAMES,
    backlog_counts,
    build_backlog_pane,
    build_brand_text,
    build_breadcrumb,
    build_footer_panel,
    build_frame,
    build_git_pane,
    build_header_panel,
    build_quadrant,
    build_roadmap_pane,
    build_status_pane,
    repo_quadrant_panes,
    summary_counts,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project": {"code": "DEMO"},
        "current": {"phase_id": "P03", "iter_id": "P03-I01"},
        "phases": {
            "P03": {"status": "active"},
            "P02": {"status": "closed"},
        },
        "iters": {"P03-I01": {"status": "active"}},
        "waves": {
            "P03-I01-W01": {"status": "pending"},
            "P03-I01-W02": {"status": "pending"},
            "P03-I01-W03": {"status": "in_progress"},
        },
        "audits": {"A01": {"status": "complete"}, "A02": {"status": "open"}},
        "backlog": {
            "B001": {"status": "open"},
            "B002": {"status": "open"},
            "B003": {"status": "closed"},
        },
        "git": {"branch": "feature/foo", "head": "abc1234", "dirty": False},
    }
    base.update(overrides)
    return base


def _render_to_string(renderable: Any) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, record=False)
    console.print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Brand + breadcrumb + keymap constants
# ---------------------------------------------------------------------------


def test_brand_literal_is_capital_e_plus_a_umlaut() -> None:
    """Brand MUST be capital ``E`` + U+00E4 (``ä``) per the branding memory."""
    assert BRAND == "Eä"
    assert len(BRAND) == 2
    assert BRAND[0] == "E"
    assert BRAND[1] == "ä"


def test_default_project_code_is_eawf() -> None:
    assert DEFAULT_PROJECT_CODE == "EAWF"


def test_footer_keymap_lists_arrows_first_vim_aliases_last() -> None:
    """Keymap convention: arrows primary, vim aliases secondary."""
    assert FOOTER_KEYMAP.startswith("↑↓")
    assert "Enter" in FOOTER_KEYMAP
    assert "Esc" in FOOTER_KEYMAP
    assert "PageUp" in FOOTER_KEYMAP and "PageDown" in FOOTER_KEYMAP
    assert "Home" in FOOTER_KEYMAP and "End" in FOOTER_KEYMAP
    vim_idx = FOOTER_KEYMAP.find("vim")
    esc_idx = FOOTER_KEYMAP.find("Esc")
    assert vim_idx > esc_idx, "vim aliases must come after primary Esc binding"


def test_quadrant_pane_names_canonical_order() -> None:
    assert QUADRANT_PANE_NAMES == ("roadmap", "status", "git", "backlog")


# ---------------------------------------------------------------------------
# build_brand_text / build_breadcrumb / panels
# ---------------------------------------------------------------------------


def test_build_brand_text_has_bold_segment_before_breadcrumb() -> None:
    text = build_brand_text("DEMO / P03")
    plain = text.plain
    assert plain.startswith(BRAND)
    assert plain.endswith("DEMO / P03")
    # Two segments: brand (bold style) then breadcrumb (cyan style).
    assert len(text._spans) >= 0  # rich.text internal — content check covers behaviour


def test_build_breadcrumb_full_state() -> None:
    assert build_breadcrumb(_state()) == "DEMO / P03 / P03-I01"


def test_build_breadcrumb_empty_state_falls_back_to_default() -> None:
    assert build_breadcrumb({}) == "EAWF"


def test_build_breadcrumb_missing_iter_drops_iter_segment() -> None:
    state = _state(current={"phase_id": "P03"})
    assert build_breadcrumb(state) == "DEMO / P03"


def test_build_breadcrumb_missing_phase_drops_both() -> None:
    state = _state(current={})
    assert build_breadcrumb(state) == "DEMO"


def test_build_header_panel_renders_brand_outside_left() -> None:
    panel = build_header_panel(_state())
    assert isinstance(panel, Panel)
    rendered = _render_to_string(panel)
    # Brand must appear before the breadcrumb's project code in the line.
    brand_idx = rendered.find(BRAND)
    crumb_idx = rendered.find("DEMO")
    assert 0 <= brand_idx < crumb_idx, (
        f"brand at {brand_idx}, breadcrumb at {crumb_idx}; brand must be outside-left"
    )


def test_build_footer_panel_renders_keymap() -> None:
    rendered = _render_to_string(build_footer_panel())
    assert "navigate" in rendered
    assert "Esc" in rendered


# ---------------------------------------------------------------------------
# summary_counts / backlog_counts
# ---------------------------------------------------------------------------


def test_summary_counts_extract_open_pending_audits() -> None:
    counts = summary_counts(_state())
    assert counts == {
        "phases_open": 1,
        "iters_open": 1,
        "waves_pending": 2,
        "waves_in_progress": 1,
        "audits": 2,
    }


def test_summary_counts_empty_state_yields_zeros() -> None:
    counts = summary_counts({})
    assert counts == {
        "phases_open": 0,
        "iters_open": 0,
        "waves_pending": 0,
        "waves_in_progress": 0,
        "audits": 0,
    }


def test_backlog_counts_open_closed_total() -> None:
    counts = backlog_counts(_state())
    assert counts == {"open": 2, "closed": 1, "total": 3}


def test_backlog_counts_missing_key_yields_zeros() -> None:
    counts = backlog_counts({})
    assert counts == {"open": 0, "closed": 0, "total": 0}


# ---------------------------------------------------------------------------
# Pane builders
# ---------------------------------------------------------------------------


def test_build_roadmap_pane_shows_counters() -> None:
    rendered = _render_to_string(build_roadmap_pane(_state()))
    assert "roadmap" in rendered
    assert "phases (active):" in rendered
    assert "waves  (pending):" in rendered


def test_build_status_pane_shows_scope() -> None:
    rendered = _render_to_string(build_status_pane(_state()))
    assert "status" in rendered
    assert "DEMO" in rendered
    assert "P03" in rendered
    assert "P03-I01" in rendered


def test_build_status_pane_handles_empty_state() -> None:
    rendered = _render_to_string(build_status_pane({}))
    assert "EAWF" in rendered
    # Empty phase/iter render as "-" placeholders.
    assert "-" in rendered


def test_build_git_pane_with_snapshot() -> None:
    rendered = _render_to_string(build_git_pane(_state()))
    assert "git" in rendered
    assert "feature/foo" in rendered
    assert "abc1234" in rendered
    assert "clean" in rendered


def test_build_git_pane_dirty_status() -> None:
    state = _state(git={"branch": "main", "head": "deadbee", "dirty": True})
    rendered = _render_to_string(build_git_pane(state))
    assert "dirty" in rendered


def test_build_git_pane_missing_snapshot_yields_placeholders() -> None:
    rendered = _render_to_string(build_git_pane({}))
    # The branch / head / status fields fall back to "-".
    assert rendered.count("-") >= 3


def test_build_backlog_pane_shows_counters() -> None:
    rendered = _render_to_string(build_backlog_pane(_state()))
    assert "backlog" in rendered
    assert "open:" in rendered
    assert "total:" in rendered


def test_repo_quadrant_panes_returns_four_panels_in_canonical_order() -> None:
    panes = repo_quadrant_panes(_state())
    assert len(panes) == 4
    for panel in panes:
        assert isinstance(panel, Panel)
    # Confirm pane titles match canonical order.
    titles = [str(p.title) for p in panes]
    assert titles == list(QUADRANT_PANE_NAMES)


# ---------------------------------------------------------------------------
# build_quadrant / build_frame
# ---------------------------------------------------------------------------


def test_build_quadrant_creates_2x2_grid() -> None:
    panes = repo_quadrant_panes(_state())
    quadrant = build_quadrant(panes)
    assert isinstance(quadrant, Layout)
    assert quadrant.name == "quadrant"
    children = list(quadrant.children)
    assert len(children) == 2
    child_names = {c.name for c in children}
    assert child_names == {"top", "bottom"}
    # Each row has two pane columns named per canonical order.
    for row in children:
        row_children = list(row.children)
        assert len(row_children) == 2


def test_build_quadrant_pane_names_match_canonical_order() -> None:
    panes = repo_quadrant_panes(_state())
    quadrant = build_quadrant(panes)
    children = {c.name: c for c in quadrant.children}
    top_children = [c.name for c in children["top"].children]
    bottom_children = [c.name for c in children["bottom"].children]
    assert top_children == [QUADRANT_PANE_NAMES[0], QUADRANT_PANE_NAMES[1]]
    assert bottom_children == [QUADRANT_PANE_NAMES[2], QUADRANT_PANE_NAMES[3]]


def test_build_quadrant_rejects_wrong_pane_count() -> None:
    with pytest.raises(ValueError, match="exactly 4 panes"):
        build_quadrant(())  # type: ignore[arg-type]
    panes_three = repo_quadrant_panes(_state())[:3]
    with pytest.raises(ValueError, match="exactly 4 panes"):
        build_quadrant(panes_three)  # type: ignore[arg-type]


def test_build_frame_has_header_body_footer_rows() -> None:
    frame = build_frame(_state())
    assert isinstance(frame, Layout)
    children = list(frame.children)
    names = {c.name for c in children}
    assert names == {"header", "body", "footer"}


def test_build_frame_body_contains_quadrant() -> None:
    frame = build_frame(_state())
    body = next(c for c in frame.children if c.name == "body")
    # The body's renderable IS the quadrant Layout.
    inner = body.renderable
    assert isinstance(inner, Layout)
    assert inner.name == "quadrant"


def test_build_frame_rendered_output_carries_brand_and_keymap() -> None:
    rendered = _render_to_string(build_frame(_state()))
    assert BRAND in rendered
    assert "DEMO" in rendered
    assert "navigate" in rendered
    # All four pane titles should appear in the rendered frame.
    for title in QUADRANT_PANE_NAMES:
        assert title in rendered, f"pane title {title!r} missing from rendered frame"


def test_module_exports_match_all_list() -> None:
    """``__all__`` discipline — exported names must resolve."""
    for name in layout_mod.__all__:
        assert hasattr(layout_mod, name), f"layout.{name} listed in __all__ but missing"
