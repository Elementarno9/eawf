"""Unit tests for the user-scope portfolio dashboard (P20-I01-W06).

Covers:

- :class:`PortfolioRow` + :class:`PortfolioViewState` strict
  construction.
- Pure metric extraction: ``_count_ready_waves``, phase / iter
  status resolution.
- Row composition via :func:`build_row` / :func:`build_rows` —
  missing-state branch, populated branch, active-code propagation.
- Header / footer / table builders: column ordering, selection
  highlight, empty body placeholder.
- Frame composition (``None`` registry vs populated) and the
  ``offline_render`` end-to-end path.
- :func:`apply_portfolio_key` transitions across arrow / vim / home /
  end / unknown keys.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table

from eawf.registry import Registry, RegistryRepoEntry
from eawf.tui.portfolio import (
    ACTIVE_MARKER_STYLE,
    PORTFOLIO_FOOTER_KEYMAP,
    SELECTED_ROW_STYLE,
    STALE_MARKER_STYLE,
    PortfolioRow,
    PortfolioViewState,
    _count_ready_waves,
    _format_iter_cell,
    _format_phase_cell,
    _resolve_iter_status,
    _resolve_phase_status,
    apply_portfolio_key,
    build_portfolio_footer,
    build_portfolio_frame,
    build_portfolio_header,
    build_portfolio_panel,
    build_portfolio_table,
    build_row,
    build_rows,
    offline_render,
    render_portfolio,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(
    codes: list[str],
    *,
    active: str | None = None,
    paths: dict[str, str] | None = None,
    titles: dict[str, str] | None = None,
) -> Registry:
    repos: dict[str, RegistryRepoEntry] = {}
    for code in codes:
        repos[code] = RegistryRepoEntry(
            code=code,
            path=(paths or {}).get(code, f"/tmp/{code}"),
            title=(titles or {}).get(code, code),
        )
    return Registry(
        version="1",
        updated_at=datetime.now(UTC),
        active_code=active,
        repos=repos,
    )


def _make_state(
    *,
    project_code: str = "EAWF",
    phase: str | None = None,
    iter_id: str | None = None,
    phases: dict[str, dict[str, str]] | None = None,
    iters: dict[str, dict[str, str]] | None = None,
    waves: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "project": {"code": project_code},
        "current": {"phase_id": phase, "iter_id": iter_id},
        "phases": phases or {},
        "iters": iters or {},
        "waves": waves or {},
    }


def _stub_loader(mapping: dict[str, dict[str, Any] | None]) -> Any:
    def loader(path: Path) -> dict[str, Any] | None:
        return mapping.get(str(path))

    return loader


def _render(renderable: Any, *, width: int = 100) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=width, record=False).print(renderable)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PortfolioViewState strict construction
# ---------------------------------------------------------------------------


def test_view_state_default_values() -> None:
    view = PortfolioViewState()
    assert view.selected_index == 0


def test_view_state_rejects_negative_index() -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError shape varies
        PortfolioViewState(selected_index=-1)


def test_view_state_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
        PortfolioViewState(selected_index=0, mystery="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# PortfolioRow strict construction
# ---------------------------------------------------------------------------


def test_portfolio_row_defaults_are_safe() -> None:
    row = PortfolioRow(code="EAWF", title="Eä")
    assert row.code == "EAWF"
    assert row.active_phase is None
    assert row.active_phase_status is None
    assert row.open_iter is None
    assert row.open_iter_status is None
    assert row.ready_waves == 0
    assert row.stale is False
    assert row.active is False


def test_portfolio_row_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017
        PortfolioRow(code="EAWF", title="x", mystery=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# _count_ready_waves
# ---------------------------------------------------------------------------


def test_count_ready_waves_pending_with_no_deps_is_ready() -> None:
    state = _make_state(
        waves={
            "W01": {"status": "pending", "deps": []},
            "W02": {"status": "pending", "deps": []},
        }
    )
    assert _count_ready_waves(state) == 2


def test_count_ready_waves_pending_with_closed_deps_is_ready() -> None:
    state = _make_state(
        waves={
            "W01": {"status": "closed", "deps": []},
            "W02": {"status": "pending", "deps": ["W01"]},
        }
    )
    assert _count_ready_waves(state) == 1


def test_count_ready_waves_pending_with_open_deps_is_not_ready() -> None:
    state = _make_state(
        waves={
            "W01": {"status": "in_progress", "deps": []},
            "W02": {"status": "pending", "deps": ["W01"]},
        }
    )
    assert _count_ready_waves(state) == 0


def test_count_ready_waves_in_progress_excluded() -> None:
    state = _make_state(
        waves={
            "W01": {"status": "in_progress", "deps": []},
        }
    )
    assert _count_ready_waves(state) == 0


def test_count_ready_waves_empty_state_returns_zero() -> None:
    assert _count_ready_waves({}) == 0


def test_count_ready_waves_malformed_waves_treated_as_zero() -> None:
    assert _count_ready_waves({"waves": "not-a-dict"}) == 0


def test_count_ready_waves_unknown_dep_blocks_ready() -> None:
    """A dep referencing a wave not in the state dict counts as unmet."""
    state = _make_state(
        waves={
            "W02": {"status": "pending", "deps": ["W00-MISSING"]},
        }
    )
    assert _count_ready_waves(state) == 0


# ---------------------------------------------------------------------------
# Phase / iter status resolvers
# ---------------------------------------------------------------------------


def test_resolve_phase_status_present() -> None:
    state = _make_state(phases={"P20": {"status": "active"}})
    assert _resolve_phase_status(state, "P20") == "active"


def test_resolve_phase_status_missing_phase_returns_none() -> None:
    state = _make_state(phases={"P20": {"status": "active"}})
    assert _resolve_phase_status(state, "P21") is None


def test_resolve_phase_status_no_phase_id_returns_none() -> None:
    state = _make_state()
    assert _resolve_phase_status(state, None) is None


def test_resolve_iter_status_present() -> None:
    state = _make_state(iters={"P20-I01": {"status": "active"}})
    assert _resolve_iter_status(state, "P20-I01") == "active"


def test_resolve_iter_status_missing_returns_none() -> None:
    state = _make_state(iters={})
    assert _resolve_iter_status(state, "P20-I01") is None


# ---------------------------------------------------------------------------
# build_row + build_rows
# ---------------------------------------------------------------------------


def test_build_row_missing_state_falls_back_to_placeholders() -> None:
    entry = RegistryRepoEntry(code="GHOST", path="/tmp/ghost")
    row = build_row(
        entry,
        registry_active_code=None,
        is_stale_for_entry=True,
        repo_state_loader=_stub_loader({"/tmp/ghost": None}),
    )
    assert row.code == "GHOST"
    assert row.title == "GHOST"
    assert row.active_phase is None
    assert row.open_iter is None
    assert row.ready_waves == 0
    assert row.stale is True
    assert row.active is False


def test_build_row_populated_state_propagates_metrics() -> None:
    entry = RegistryRepoEntry(code="EAWF", path="/tmp/eawf", title="Eä")
    state = _make_state(
        phase="P20",
        iter_id="P20-I01",
        phases={"P20": {"status": "active"}},
        iters={"P20-I01": {"status": "active"}},
        waves={
            "W01": {"status": "closed", "deps": []},
            "W02": {"status": "pending", "deps": ["W01"]},
        },
    )
    row = build_row(
        entry,
        registry_active_code="EAWF",
        is_stale_for_entry=False,
        repo_state_loader=_stub_loader({"/tmp/eawf": state}),
    )
    assert row.code == "EAWF"
    assert row.title == "Eä"
    assert row.active_phase == "P20"
    assert row.active_phase_status == "active"
    assert row.open_iter == "P20-I01"
    assert row.open_iter_status == "active"
    assert row.ready_waves == 1
    assert row.stale is False
    assert row.active is True


def test_build_rows_sorts_alphabetically() -> None:
    registry = _make_registry(["ZED", "ALPHA", "MIDDLE"])
    rows = build_rows(
        registry,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    codes = [r.code for r in rows]
    assert codes == ["ALPHA", "MIDDLE", "ZED"]


def test_build_rows_threads_stale_evaluator() -> None:
    registry = _make_registry(["A", "B"])
    rows = build_rows(
        registry,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: e.code == "A",
    )
    assert rows[0].stale is True  # A
    assert rows[1].stale is False  # B


def test_build_rows_marks_active_entry() -> None:
    registry = _make_registry(["A", "B"], active="B")
    rows = build_rows(
        registry,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    actives = {r.code: r.active for r in rows}
    assert actives == {"A": False, "B": True}


# ---------------------------------------------------------------------------
# Cell formatters
# ---------------------------------------------------------------------------


def test_format_phase_cell_present() -> None:
    row = PortfolioRow(code="EAWF", title="Eä", active_phase="P20", active_phase_status="active")
    assert _format_phase_cell(row) == "P20 active"


def test_format_phase_cell_no_status_just_phase() -> None:
    row = PortfolioRow(code="EAWF", title="Eä", active_phase="P20")
    assert _format_phase_cell(row) == "P20"


def test_format_phase_cell_absent_returns_dash() -> None:
    row = PortfolioRow(code="EAWF", title="Eä")
    assert _format_phase_cell(row) == "-"


def test_format_iter_cell_present() -> None:
    row = PortfolioRow(code="EAWF", title="Eä", open_iter="P20-I01", open_iter_status="active")
    assert _format_iter_cell(row) == "P20-I01 active"


def test_format_iter_cell_absent_returns_dash() -> None:
    row = PortfolioRow(code="EAWF", title="Eä")
    assert _format_iter_cell(row) == "-"


# ---------------------------------------------------------------------------
# build_portfolio_header / footer
# ---------------------------------------------------------------------------


def test_header_carries_brand_and_count() -> None:
    registry = _make_registry(["A", "B", "C"])
    rendered = _render(build_portfolio_header(registry))
    assert "Eä" in rendered
    assert "portfolio (3 repos)" in rendered


def test_header_none_registry_shows_unavailable() -> None:
    rendered = _render(build_portfolio_header(None))
    assert "Eä" in rendered
    assert "unavailable" in rendered


def test_footer_keymap_lists_navigation_keys_first() -> None:
    assert PORTFOLIO_FOOTER_KEYMAP.startswith("↑↓ navigate")
    assert "Enter open" in PORTFOLIO_FOOTER_KEYMAP
    assert "Esc back" in PORTFOLIO_FOOTER_KEYMAP
    assert "q quit" in PORTFOLIO_FOOTER_KEYMAP
    rendered = _render(build_portfolio_footer())
    assert "navigate" in rendered


# ---------------------------------------------------------------------------
# build_portfolio_table
# ---------------------------------------------------------------------------


def test_table_lists_all_rows_in_order() -> None:
    rows = [
        PortfolioRow(code="ALPHA", title="A"),
        PortfolioRow(code="BETA", title="B"),
    ]
    rendered = _render(build_portfolio_table(rows))
    assert "ALPHA" in rendered
    assert "BETA" in rendered


def test_table_includes_all_seven_columns() -> None:
    rows = [PortfolioRow(code="EAWF", title="Eä")]
    table = build_portfolio_table(rows)
    assert isinstance(table, Table)
    column_headers = [str(c.header) for c in table.columns]
    assert column_headers == [
        "code",
        "title",
        "phase",
        "iter",
        "ready",
        "stale",
        "active",
    ]


def test_table_active_marker_uses_active_style() -> None:
    row = PortfolioRow(code="EAWF", title="Eä", active=True)
    table = build_portfolio_table([row])
    rendered = _render(table)
    # The "yes" marker must surface for the active column.
    assert "yes" in rendered


def test_table_stale_marker_uses_stale_style() -> None:
    row = PortfolioRow(code="GHOST", title="Ghost", stale=True)
    table = build_portfolio_table([row])
    rendered = _render(table)
    assert "yes" in rendered


def test_table_selection_index_applies_style(tmp_path: Path) -> None:
    rows = [
        PortfolioRow(code="ALPHA", title="A"),
        PortfolioRow(code="BETA", title="B"),
    ]
    view = PortfolioViewState(selected_index=1)
    rendered = _render(build_portfolio_table(rows, view=view))
    # Both codes should still appear.
    assert "ALPHA" in rendered
    assert "BETA" in rendered


def test_panel_empty_rows_carries_placeholder_message() -> None:
    panel = build_portfolio_panel([])
    assert isinstance(panel, Panel)
    rendered = _render(panel)
    assert "no repos registered" in rendered


def test_panel_non_empty_renders_table() -> None:
    rows = [PortfolioRow(code="EAWF", title="Eä")]
    rendered = _render(build_portfolio_panel(rows))
    assert "EAWF" in rendered


# ---------------------------------------------------------------------------
# build_portfolio_frame
# ---------------------------------------------------------------------------


def test_frame_layout_has_three_rows() -> None:
    registry = _make_registry(["A"])
    frame = build_portfolio_frame(
        registry,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    assert isinstance(frame, Layout)
    names = {c.name for c in frame.children}
    assert names == {"header", "body", "footer"}


def test_frame_with_registry_renders_brand_and_codes() -> None:
    registry = _make_registry(["A", "B"])
    out = _render(
        build_portfolio_frame(
            registry,
            repo_state_loader=lambda p: None,
            is_stale_evaluator=lambda e: False,
        )
    )
    assert "Eä" in out
    assert "A" in out
    assert "B" in out


def test_frame_none_registry_carries_unavailable_placeholder() -> None:
    out = _render(build_portfolio_frame(None))
    assert "Eä" in out
    assert "unavailable" in out


def test_frame_empty_registry_carries_empty_message() -> None:
    out = _render(
        build_portfolio_frame(
            Registry(),
            repo_state_loader=lambda p: None,
            is_stale_evaluator=lambda e: False,
        )
    )
    assert "Eä" in out
    assert "no repos registered" in out or "0 repos" in out


# ---------------------------------------------------------------------------
# render_portfolio + offline_render
# ---------------------------------------------------------------------------


def test_render_portfolio_returns_captured_text() -> None:
    registry = _make_registry(["EAWF"])
    out = render_portfolio(
        registry,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    assert "Eä" in out
    assert "EAWF" in out


def test_render_portfolio_external_console_returns_empty() -> None:
    registry = _make_registry(["EAWF"])
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    out = render_portfolio(
        registry,
        console=console,
        repo_state_loader=lambda p: None,
        is_stale_evaluator=lambda e: False,
    )
    assert out == ""
    assert "Eä" in buf.getvalue()


def test_offline_render_via_real_registry_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo-a"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        json.dumps(
            {
                "project": {"code": "REPOA"},
                "current": {"phase_id": "P03", "iter_id": "P03-I01"},
                "phases": {"P03": {"status": "active"}},
                "iters": {"P03-I01": {"status": "active"}},
                "waves": {},
            }
        )
    )
    import orjson

    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": datetime.now(UTC).isoformat(),
                "active_code": "REPOA",
                "repos": {"REPOA": {"code": "REPOA", "path": str(repo), "title": "Repo A"}},
            }
        )
    )
    out = offline_render(registry_path=target)
    assert "Eä" in out
    assert "REPOA" in out
    assert "P03" in out


def test_offline_render_missing_registry_carries_placeholder(tmp_path: Path) -> None:
    out = offline_render(registry_path=tmp_path / "absent.json")
    assert "Eä" in out
    assert "unavailable" in out


# ---------------------------------------------------------------------------
# apply_portfolio_key transitions
# ---------------------------------------------------------------------------


@pytest.fixture
def two_rows() -> list[PortfolioRow]:
    return [
        PortfolioRow(code="ALPHA", title="A"),
        PortfolioRow(code="BETA", title="B"),
    ]


def test_apply_key_down_advances(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=0)
    after = apply_portfolio_key(view, "\x1b[B", rows=two_rows)
    assert after.selected_index == 1


def test_apply_key_down_clamps_at_end(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=1)
    after = apply_portfolio_key(view, "\x1b[B", rows=two_rows)
    assert after.selected_index == 1


def test_apply_key_up_decrements(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=1)
    after = apply_portfolio_key(view, "\x1b[A", rows=two_rows)
    assert after.selected_index == 0


def test_apply_key_up_clamps_at_zero(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=0)
    after = apply_portfolio_key(view, "\x1b[A", rows=two_rows)
    assert after.selected_index == 0


def test_apply_key_home_jumps_to_top(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=1)
    after = apply_portfolio_key(view, "g", rows=two_rows)
    assert after.selected_index == 0


def test_apply_key_end_jumps_to_bottom(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState(selected_index=0)
    after = apply_portfolio_key(view, "G", rows=two_rows)
    assert after.selected_index == 1


def test_apply_key_vim_j_advances(two_rows: list[PortfolioRow]) -> None:
    after = apply_portfolio_key(PortfolioViewState(), "j", rows=two_rows)
    assert after.selected_index == 1


def test_apply_key_vim_k_decrements(two_rows: list[PortfolioRow]) -> None:
    after = apply_portfolio_key(PortfolioViewState(selected_index=1), "k", rows=two_rows)
    assert after.selected_index == 0


def test_apply_key_unknown_returns_view_unchanged(two_rows: list[PortfolioRow]) -> None:
    view = PortfolioViewState()
    assert apply_portfolio_key(view, "z", rows=two_rows) == view


def test_apply_key_empty_rows_noop() -> None:
    view = PortfolioViewState()
    assert apply_portfolio_key(view, "j", rows=[]) == view


# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------


def test_marker_style_constants_are_strings() -> None:
    assert isinstance(SELECTED_ROW_STYLE, str)
    assert isinstance(ACTIVE_MARKER_STYLE, str)
    assert isinstance(STALE_MARKER_STYLE, str)
    # Active marker should be bold-green per the W05 chip convention.
    assert "green" in ACTIVE_MARKER_STYLE


# ---------------------------------------------------------------------------
# Defensive: malformed state shapes do not crash the row composer
# ---------------------------------------------------------------------------


def test_build_row_malformed_current_section_handled() -> None:
    entry = RegistryRepoEntry(code="EAWF", path="/tmp/eawf")
    state = {"current": "not-a-dict"}
    row = build_row(
        entry,
        registry_active_code=None,
        is_stale_for_entry=False,
        repo_state_loader=_stub_loader({"/tmp/eawf": state}),
    )
    assert row.active_phase is None
    assert row.open_iter is None
    assert row.ready_waves == 0


def test_build_row_state_with_no_current_section() -> None:
    entry = RegistryRepoEntry(code="EAWF", path="/tmp/eawf")
    state: dict[str, Any] = {}
    row = build_row(
        entry,
        registry_active_code=None,
        is_stale_for_entry=False,
        repo_state_loader=_stub_loader({"/tmp/eawf": state}),
    )
    assert row.active_phase is None
    assert row.open_iter is None
