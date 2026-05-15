"""Unit tests for the rich layout helpers (P20-I01-W02; P20-I03-W01 refresh).

Covers the brand/breadcrumb/keymap constants, the four pane builders,
the 2x2 quadrant composer, and the header+body+footer frame builder
exported by :mod:`eawf.tui.layout`. P20-I03-W01 adds coverage for the
live ``git`` CLI pane, the new ``iters (closed)`` roadmap field, the
latest-iter surfacing in the status pane, the rewritten quadrant
footer keymap, and the overlay-pending footer override.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from eawf.tui import layout as layout_mod
from eawf.tui.layout import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    FOOTER_KEYMAP,
    FOOTER_KEYMAP_OVERLAY_PENDING,
    GIT_PANE_CACHE_TTL,
    QUADRANT_PANE_NAMES,
    _reset_git_pane_cache,
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
        "iters": {
            "P03-I01": {"status": "active"},
            "P02-I01": {"status": "closed", "closed_at": "2026-04-10T00:00:00+00:00"},
        },
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
    }
    base.update(overrides)
    return base


def _render_to_string(renderable: Any, *, width: int = 100) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    console.print(renderable)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clear_git_cache_between_tests() -> None:
    """Reset the git-pane shell-out cache between assertions.

    Prevents one test's monkeypatched fields from leaking into the next.
    """
    _reset_git_pane_cache()


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


def test_quadrant_footer_keymap_lists_board_config_overlay_quit() -> None:
    """P20-I03-W01: quadrant footer carries quadrant-level keys.

    The pre-W01 string was wave-board-shaped (arrows + paging + Enter)
    which leaked board keys into the quadrant. The new string only
    advertises the keys the quadrant actually handles.
    """
    assert "b board" in FOOTER_KEYMAP
    assert "c config" in FOOTER_KEYMAP
    assert "oH/oD/oM/oE/oR overlay" in FOOTER_KEYMAP
    assert "Esc/q quit" in FOOTER_KEYMAP
    # The wave-board keys must NOT be advertised here — they live in
    # wave_board.py.
    assert "PageUp" not in FOOTER_KEYMAP
    assert "navigate" not in FOOTER_KEYMAP


def test_overlay_pending_footer_lists_overlay_objects() -> None:
    assert "H hypothesis" in FOOTER_KEYMAP_OVERLAY_PENDING
    assert "D decision" in FOOTER_KEYMAP_OVERLAY_PENDING
    assert "M memory" in FOOTER_KEYMAP_OVERLAY_PENDING
    assert "E events" in FOOTER_KEYMAP_OVERLAY_PENDING
    assert "R dispatch" in FOOTER_KEYMAP_OVERLAY_PENDING
    assert "Esc cancel" in FOOTER_KEYMAP_OVERLAY_PENDING


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
    brand_idx = rendered.find(BRAND)
    crumb_idx = rendered.find("DEMO")
    assert 0 <= brand_idx < crumb_idx, (
        f"brand at {brand_idx}, breadcrumb at {crumb_idx}; brand must be outside-left"
    )


def test_build_footer_panel_renders_keymap() -> None:
    rendered = _render_to_string(build_footer_panel())
    # New quadrant keymap fragments.
    assert "board" in rendered
    assert "overlay" in rendered
    assert "Esc" in rendered


def test_build_footer_panel_overlay_pending_override() -> None:
    rendered = _render_to_string(build_footer_panel(keymap=FOOTER_KEYMAP_OVERLAY_PENDING))
    assert "hypothesis" in rendered
    assert "Esc cancel" in rendered


# ---------------------------------------------------------------------------
# summary_counts / backlog_counts
# ---------------------------------------------------------------------------


def test_summary_counts_extract_open_pending_audits() -> None:
    counts = summary_counts(_state())
    assert counts == {
        "phases_open": 1,
        "iters_open": 1,
        "iters_closed": 1,
        "waves_pending": 2,
        "waves_in_progress": 1,
        "audits": 2,
    }


def test_summary_counts_empty_state_yields_zeros() -> None:
    counts = summary_counts({})
    assert counts == {
        "phases_open": 0,
        "iters_open": 0,
        "iters_closed": 0,
        "waves_pending": 0,
        "waves_in_progress": 0,
        "audits": 0,
    }


def test_summary_counts_tracks_closed_iters() -> None:
    """P20-I03-W01: roadmap pane needs historical iter count."""
    state = _state(
        iters={
            "P03-I01": {"status": "active"},
            "P02-I01": {"status": "closed"},
            "P02-I02": {"status": "closed"},
            "P01-I01": {"status": "closed"},
        }
    )
    counts = summary_counts(state)
    assert counts["iters_open"] == 1
    assert counts["iters_closed"] == 3


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
    assert "iters  (active):" in rendered
    # P20-I03-W01: iters (closed) lands underneath iters (active).
    assert "iters  (closed):" in rendered
    assert "waves  (pending):" in rendered


def test_build_status_pane_shows_active_iter() -> None:
    rendered = _render_to_string(build_status_pane(_state()))
    assert "status" in rendered
    assert "DEMO" in rendered
    assert "P03" in rendered
    assert "P03-I01" in rendered
    # Active-iter line carries status suffix.
    assert "(active)" in rendered


def test_build_status_pane_surfaces_latest_iter_when_none_active() -> None:
    """P20-I03-W01: when no iter is active, surface the most recent.

    The operator opens the TUI after the active iter closes and sees
    the historical iter id rather than a bare dash.
    """
    state = _state(
        current={"phase_id": "P20", "iter_id": None},
        iters={
            "P20-I01": {"status": "closed", "closed_at": "2026-05-01T12:00:00+00:00"},
            "P20-I02": {"status": "closed", "closed_at": "2026-05-08T09:30:00+00:00"},
            "P20-I03": {"status": "closed", "closed_at": "2026-05-14T18:00:00+00:00"},
        },
    )
    rendered = _render_to_string(build_status_pane(state))
    assert "P20-I03" in rendered
    assert "2026-05-14" in rendered
    # The bare-dash form must not appear when we have a record to show.
    assert "(no iter started)" not in rendered


def test_build_status_pane_handles_no_iters_at_all() -> None:
    state = _state(
        current={"phase_id": "P20", "iter_id": None},
        iters={"P20-I01": {"status": "planned"}},
    )
    rendered = _render_to_string(build_status_pane(state))
    assert "(no iter started)" in rendered


def test_build_status_pane_handles_empty_state() -> None:
    rendered = _render_to_string(build_status_pane({}))
    assert "EAWF" in rendered
    # No iters at all renders the deliberate placeholder.
    assert "(no iter started)" in rendered


def test_build_backlog_pane_shows_counters() -> None:
    rendered = _render_to_string(build_backlog_pane(_state()))
    assert "backlog" in rendered
    assert "open:" in rendered
    assert "total:" in rendered


# ---------------------------------------------------------------------------
# Git pane — live shell-out (P20-I03-W01 success criterion 1)
# ---------------------------------------------------------------------------


def _fake_completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_build_git_pane_reads_live_branch_head_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean repo: branch + short SHA + ``clean`` status surface."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        # args is [`git`, ...]
        calls.append(args[1:])
        if args[1:2] == ["rev-parse"] and "--abbrev-ref" in args:
            return _fake_completed(0, "feature/eawf-v0.3-p20")
        if args[1:2] == ["rev-parse"] and "--short" in args:
            return _fake_completed(0, "abc1234")
        if args[1:2] == ["status"]:
            return _fake_completed(0, "")
        if args[1:2] == ["rev-list"]:
            return _fake_completed(0, "0")
        return _fake_completed(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(build_git_pane({}, workspace=Path("/tmp/anything")))
    assert "git" in rendered
    assert "feature/eawf-v0.3-p20" in rendered
    assert "abc1234" in rendered
    assert "clean" in rendered


def test_build_git_pane_dirty_status_counts_modified_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three porcelain lines render as ``3 modified``."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "main")
        if "--short" in args:
            return _fake_completed(0, "deadbee")
        if "status" in args:
            return _fake_completed(0, " M file1.py\n?? file2.py\n M file3.py\n")
        return _fake_completed(1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(build_git_pane({}, workspace=Path("/tmp/anywhere")))
    assert "3 modified" in rendered
    assert "main" in rendered


def test_build_git_pane_no_upstream_renders_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No upstream → ``upstream: -`` rather than crash."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "feature/foo")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        # rev-list against ``@{u}`` fails when no upstream.
        return _fake_completed(128, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(build_git_pane({}, workspace=Path("/tmp/anywhere")))
    assert "upstream: -" in rendered


def test_build_git_pane_no_git_binary_renders_all_dashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git`` not on PATH → every field renders a dash, no crash."""

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(build_git_pane({}, workspace=Path("/tmp/anywhere")))
    # All four field labels render a dash.
    assert "branch:   -" in rendered
    assert "head:     -" in rendered
    assert "status:   -" in rendered
    assert "upstream: -" in rendered


def test_build_git_pane_subprocess_timeout_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung git process times out and renders a dash for that field."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)
        if "--short" in args:
            return _fake_completed(0, "feedface")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(build_git_pane({}, workspace=Path("/tmp/anywhere")))
    assert "branch:   -" in rendered
    # Other fields still resolve.
    assert "feedface" in rendered


def test_build_git_pane_caches_subprocess_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two renders within :data:`GIT_PANE_CACHE_TTL` share one shell-out."""
    counter = {"calls": 0}

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        counter["calls"] += 1
        if "--abbrev-ref" in args:
            return _fake_completed(0, "branchA")
        if "--short" in args:
            return _fake_completed(0, "1234567")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = Path("/tmp/cache-test")
    build_git_pane({}, workspace=workspace)
    first_call_count = counter["calls"]
    # Second render in the same monotonic-second hits the cache.
    build_git_pane({}, workspace=workspace)
    extra = counter["calls"] - first_call_count
    assert counter["calls"] == first_call_count, (
        f"expected cached fields; subprocess was called {extra} extra time(s)"
    )


def test_build_git_pane_cache_expires_past_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monkeypatched :func:`time.monotonic` jump past TTL re-runs git."""
    counter = {"calls": 0}

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        counter["calls"] += 1
        if "--abbrev-ref" in args:
            return _fake_completed(0, "branchA")
        if "--short" in args:
            return _fake_completed(0, "1234567")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workspace = Path("/tmp/ttl-test")
    # Inject a known monotonic clock so the cache decision is exact.
    with patch.object(layout_mod, "_git_pane_fields", wraps=layout_mod._git_pane_fields) as _:
        layout_mod._git_pane_fields(workspace, now=0.0)
        first_calls = counter["calls"]
        # Same monotonic tick — should be a cache hit.
        layout_mod._git_pane_fields(workspace, now=GIT_PANE_CACHE_TTL / 2)
        assert counter["calls"] == first_calls
        # Past the TTL — refresh.
        layout_mod._git_pane_fields(workspace, now=GIT_PANE_CACHE_TTL * 2 + 1.0)
        assert counter["calls"] > first_calls


def test_build_git_pane_ignores_state_git_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P20-I03-W01: stale ``state['git']`` is shadowed by the live read."""

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if "--abbrev-ref" in args:
            return _fake_completed(0, "live-branch")
        if "--short" in args:
            return _fake_completed(0, "abc1234")
        if "status" in args:
            return _fake_completed(0, "")
        return _fake_completed(0, "0")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rendered = _render_to_string(
        build_git_pane(
            {"git": {"branch": "stale-snapshot", "head": "STALE", "dirty": True}},
            workspace=Path("/tmp/anywhere"),
        )
    )
    assert "live-branch" in rendered
    assert "stale-snapshot" not in rendered


# ---------------------------------------------------------------------------
# Quadrant / frame
# ---------------------------------------------------------------------------


def test_repo_quadrant_panes_returns_four_panels_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    panes = repo_quadrant_panes(_state())
    assert len(panes) == 4
    for panel in panes:
        assert isinstance(panel, Panel)
    titles = [str(p.title) for p in panes]
    assert titles == list(QUADRANT_PANE_NAMES)


def test_build_quadrant_creates_2x2_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    panes = repo_quadrant_panes(_state())
    quadrant = build_quadrant(panes)
    assert isinstance(quadrant, Layout)
    assert quadrant.name == "quadrant"
    children = list(quadrant.children)
    assert len(children) == 2
    child_names = {c.name for c in children}
    assert child_names == {"top", "bottom"}
    for row in children:
        row_children = list(row.children)
        assert len(row_children) == 2


def test_build_quadrant_pane_names_match_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    panes = repo_quadrant_panes(_state())
    quadrant = build_quadrant(panes)
    children = {c.name: c for c in quadrant.children}
    top_children = [c.name for c in children["top"].children]
    bottom_children = [c.name for c in children["bottom"].children]
    assert top_children == [QUADRANT_PANE_NAMES[0], QUADRANT_PANE_NAMES[1]]
    assert bottom_children == [QUADRANT_PANE_NAMES[2], QUADRANT_PANE_NAMES[3]]


def test_build_quadrant_rejects_wrong_pane_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    with pytest.raises(ValueError, match="exactly 4 panes"):
        build_quadrant(())  # type: ignore[arg-type]
    panes_three = repo_quadrant_panes(_state())[:3]
    with pytest.raises(ValueError, match="exactly 4 panes"):
        build_quadrant(panes_three)  # type: ignore[arg-type]


def test_build_frame_has_header_body_footer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    frame = build_frame(_state())
    assert isinstance(frame, Layout)
    children = list(frame.children)
    names = {c.name for c in children}
    assert names == {"header", "body", "footer"}


def test_build_frame_body_contains_quadrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    frame = build_frame(_state())
    body = next(c for c in frame.children if c.name == "body")
    inner = body.renderable
    assert isinstance(inner, Layout)
    assert inner.name == "quadrant"


def test_build_frame_rendered_output_carries_brand_and_keymap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    rendered = _render_to_string(build_frame(_state()))
    assert BRAND in rendered
    assert "DEMO" in rendered
    # New quadrant keymap fragment.
    assert "board" in rendered
    assert "overlay" in rendered
    # All four pane titles should appear in the rendered frame.
    for title in QUADRANT_PANE_NAMES:
        assert title in rendered, f"pane title {title!r} missing from rendered frame"


def test_build_frame_footer_override_renders_overlay_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _fake_completed(0))
    rendered = _render_to_string(build_frame(_state(), footer_keymap=FOOTER_KEYMAP_OVERLAY_PENDING))
    assert "hypothesis" in rendered
    assert "Esc cancel" in rendered
    # The default keymap must NOT also be present when overridden.
    assert "b board" not in rendered


def test_module_exports_match_all_list() -> None:
    """``__all__`` discipline — exported names must resolve."""
    for name in layout_mod.__all__:
        assert hasattr(layout_mod, name), f"layout.{name} listed in __all__ but missing"
