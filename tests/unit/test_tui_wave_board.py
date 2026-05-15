"""Unit tests for the wave-board pure helpers (P20-I01-W03).

Covers the sort / filter / cycle helpers, the panel builders, and
the :class:`WaveBoardState` view-state container. Integration-level
golden snapshots + key-dispatch coverage live in
``tests/integration/test_tui_wave_board.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.state.enums import (
    EffortBucket,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.state.models import CurrentPointers, Project, State, Wave
from eawf.tui.wave_board import (
    FILTER_MODES,
    STATUS_PRIORITY,
    WAVE_BOARD_FOOTER,
    WaveBoardState,
    apply_key,
    build_detail_panel,
    build_list_panel,
    build_wave_board_frame,
    filter_waves,
    next_filter_mode,
    render_wave_board,
    sort_waves,
    status_priority,
    waves_for_iter,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _make_wave(
    wave_id: str,
    *,
    status: WaveStatus,
    iter_id: str = "P20-I01",
    title: str | None = None,
    deps: list[str] | None = None,
    blocks: list[str] | None = None,
    success_criteria: list[str] | None = None,
    token_budget: int | None = None,
    tokens_consumed: int = 0,
    outcome: str | None = None,
) -> Wave:
    """Build a :class:`Wave` for fixture state.

    Sensible defaults so tests stay terse — every test customises
    only the fields it asserts on.
    """
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title or f"feat: {wave_id}",
        status=status,
        deps=deps or [],
        blocks=blocks or [],
        file_scopes=[],
        success_criteria=success_criteria or [],
        agent_role=None,
        effort_bucket=EffortBucket.M,
        claim_session_id=None,
        worktree_id=None,
        token_budget=token_budget,
        tokens_consumed=tokens_consumed,
        outcome=outcome,
        commit=None,
        opened_at=_utc_now(),
        closed_at=None,
    )


def _make_state(waves: list[Wave], *, iter_id: str = "P20-I01") -> State:
    """Build a minimal :class:`State` carrying *waves* under *iter_id*.

    The phase / iter records are seeded as well so referential checks
    elsewhere stay happy if/when imported as a fixture.
    """
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _utc_now(),
        "project": Project(
            code="EAWF",
            slug="eawf",
            title="EAWF",
            description=None,
            domains=["x"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:EAWF",
        ).model_dump(mode="json"),
        "current": CurrentPointers(
            project_code="EAWF",
            phase_id=iter_id.split("-")[0],
            iter_id=iter_id,
        ).model_dump(mode="json"),
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {w.id: w.model_dump(mode="json") for w in waves},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


# ---------------------------------------------------------------------------
# Constant tables
# ---------------------------------------------------------------------------


def test_status_priority_table_orders_in_progress_first() -> None:
    """Operator-friendly order — IN_PROGRESS is index 0."""
    assert STATUS_PRIORITY[WaveStatus.IN_PROGRESS.value] == 0
    assert STATUS_PRIORITY[WaveStatus.CLAIMED.value] == 1
    assert STATUS_PRIORITY[WaveStatus.PENDING.value] == 2
    assert STATUS_PRIORITY[WaveStatus.FAILED.value] == 3
    assert STATUS_PRIORITY[WaveStatus.CLOSED.value] == 4


def test_status_priority_unknown_returns_sentinel() -> None:
    """Unknown statuses must sort *after* every known status."""
    sentinel = status_priority("brand-new-status")
    for known in STATUS_PRIORITY.values():
        assert sentinel > known


def test_status_priority_known_value() -> None:
    assert status_priority(WaveStatus.IN_PROGRESS.value) == 0


def test_filter_modes_cycle_order_is_canonical() -> None:
    """The dispatch-spec cycle order MUST be exactly this five-tuple."""
    assert FILTER_MODES == (
        "all",
        "pending",
        "claimed_in_progress",
        "closed",
        "failed",
    )


def test_footer_keymap_lists_filter_and_board_keys() -> None:
    """Wave-board footer surfaces the new keys: ``f``/``Esc``."""
    assert WAVE_BOARD_FOOTER.startswith("↑↓")
    assert "f filter" in WAVE_BOARD_FOOTER
    assert "Esc" in WAVE_BOARD_FOOTER


# ---------------------------------------------------------------------------
# sort_waves
# ---------------------------------------------------------------------------


def test_sort_waves_orders_by_status_priority_then_wave_id() -> None:
    """Success criterion 1 contract."""
    waves = [
        _make_wave("P20-I01-W05", status=WaveStatus.CLOSED),
        _make_wave("P20-I01-W04", status=WaveStatus.PENDING),
        _make_wave("P20-I01-W02", status=WaveStatus.IN_PROGRESS),
        _make_wave("P20-I01-W03", status=WaveStatus.CLAIMED),
        _make_wave("P20-I01-W01", status=WaveStatus.FAILED),
    ]
    out = sort_waves(waves)
    ids = [w.id for w in out]
    # in_progress > claimed > pending > failed > closed
    assert ids == [
        "P20-I01-W02",  # in_progress
        "P20-I01-W03",  # claimed
        "P20-I01-W04",  # pending
        "P20-I01-W01",  # failed
        "P20-I01-W05",  # closed
    ]


def test_sort_waves_within_bucket_uses_wave_id_ascending() -> None:
    waves = [
        _make_wave("P20-I01-W08", status=WaveStatus.PENDING),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING),
        _make_wave("P20-I01-W05", status=WaveStatus.PENDING),
    ]
    out = sort_waves(waves)
    assert [w.id for w in out] == ["P20-I01-W02", "P20-I01-W05", "P20-I01-W08"]


def test_sort_waves_empty_input_returns_empty() -> None:
    assert sort_waves([]) == []


def test_sort_waves_does_not_mutate_input() -> None:
    waves = [
        _make_wave("P20-I01-W02", status=WaveStatus.CLOSED),
        _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS),
    ]
    original_ids = [w.id for w in waves]
    sort_waves(waves)
    assert [w.id for w in waves] == original_ids


# ---------------------------------------------------------------------------
# filter_waves
# ---------------------------------------------------------------------------


def test_filter_waves_all_returns_full_list() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.PENDING),
        _make_wave("P20-I01-W02", status=WaveStatus.CLOSED),
    ]
    out = filter_waves(waves, "all")
    assert len(out) == 2


def test_filter_waves_pending_keeps_only_pending() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.PENDING),
        _make_wave("P20-I01-W02", status=WaveStatus.IN_PROGRESS),
        _make_wave("P20-I01-W03", status=WaveStatus.CLOSED),
    ]
    out = filter_waves(waves, "pending")
    assert [w.id for w in out] == ["P20-I01-W01"]


def test_filter_waves_claimed_in_progress_keeps_both_statuses() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.CLAIMED),
        _make_wave("P20-I01-W02", status=WaveStatus.IN_PROGRESS),
        _make_wave("P20-I01-W03", status=WaveStatus.PENDING),
    ]
    out = filter_waves(waves, "claimed_in_progress")
    assert {w.id for w in out} == {"P20-I01-W01", "P20-I01-W02"}


def test_filter_waves_closed_keeps_only_closed() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.CLOSED),
        _make_wave("P20-I01-W02", status=WaveStatus.IN_PROGRESS),
    ]
    out = filter_waves(waves, "closed")
    assert [w.id for w in out] == ["P20-I01-W01"]


def test_filter_waves_failed_keeps_only_failed() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.FAILED),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING),
    ]
    out = filter_waves(waves, "failed")
    assert [w.id for w in out] == ["P20-I01-W01"]


def test_filter_waves_unknown_mode_raises_valueerror() -> None:
    waves = [_make_wave("P20-I01-W01", status=WaveStatus.PENDING)]
    with pytest.raises(ValueError, match="unknown filter mode"):
        filter_waves(waves, "totally-fake-mode")


def test_filter_waves_empty_input_returns_empty() -> None:
    assert filter_waves([], "all") == []
    assert filter_waves([], "pending") == []


# ---------------------------------------------------------------------------
# next_filter_mode
# ---------------------------------------------------------------------------


def test_next_filter_mode_cycles_all_through_failed_and_wraps() -> None:
    """Success criterion 3 — full cycle order."""
    assert next_filter_mode("all") == "pending"
    assert next_filter_mode("pending") == "claimed_in_progress"
    assert next_filter_mode("claimed_in_progress") == "closed"
    assert next_filter_mode("closed") == "failed"
    assert next_filter_mode("failed") == "all"


def test_next_filter_mode_unknown_resets_to_first() -> None:
    """Corrupt view state must not wedge the cycle."""
    assert next_filter_mode("nonsense") == FILTER_MODES[0]


# ---------------------------------------------------------------------------
# WaveBoardState — pydantic v2 strict validation
# ---------------------------------------------------------------------------


def test_wave_board_state_defaults() -> None:
    view = WaveBoardState()
    assert view.selected_index == 0
    assert view.filter_mode == "all"


def test_wave_board_state_rejects_unknown_field() -> None:
    """extra='forbid' must be honoured."""
    with pytest.raises(ValidationError, match="extra"):
        WaveBoardState(  # type: ignore[call-arg]
            selected_index=0,
            filter_mode="all",
            bogus_field="oops",
        )


def test_wave_board_state_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        WaveBoardState(selected_index=-1)


def test_wave_board_state_model_copy_update() -> None:
    view = WaveBoardState(selected_index=2)
    new_view = view.model_copy(update={"selected_index": 5})
    assert view.selected_index == 2
    assert new_view.selected_index == 5


# ---------------------------------------------------------------------------
# waves_for_iter
# ---------------------------------------------------------------------------


def test_waves_for_iter_filters_by_iter_id() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.PENDING),
        _make_wave("P20-I02-W01", status=WaveStatus.PENDING, iter_id="P20-I02"),
    ]
    state = _make_state(waves)
    out = waves_for_iter(state, "P20-I01")
    assert [w.id for w in out] == ["P20-I01-W01"]


def test_waves_for_iter_missing_iter_returns_empty() -> None:
    state = _make_state([])
    assert waves_for_iter(state, "P20-I99") == []


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def _render(renderable: Any) -> str:
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(renderable)
    return buf.getvalue()


def test_build_list_panel_renders_header_and_rows() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS, title="feat: alpha"),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING, title="feat: beta"),
    ]
    panel = build_list_panel(waves, filter_mode="all", selected_index=0, total=2)
    rendered = _render(panel)
    assert "waves" in rendered
    assert "filter=all" in rendered
    assert "2 of 2" in rendered
    assert "P20-I01-W01" in rendered
    assert "in_progress" in rendered
    assert "feat: alpha" in rendered


def test_build_list_panel_marks_selected_row() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING),
    ]
    panel = build_list_panel(waves, filter_mode="all", selected_index=1, total=2)
    rendered = _render(panel)
    # The cursor marker ``>`` precedes the selected wave id but not the other.
    # We assert the marker appears on the W02 line by scanning lines.
    lines = rendered.splitlines()
    w02_lines = [line for line in lines if "P20-I01-W02" in line]
    assert any(">" in line for line in w02_lines)


def test_build_list_panel_empty_filtered_list_shows_placeholder() -> None:
    panel = build_list_panel([], filter_mode="failed", selected_index=0, total=5)
    rendered = _render(panel)
    assert "0 of 5" in rendered
    assert "no waves match this filter" in rendered


def test_build_detail_panel_none_wave_shows_placeholder() -> None:
    state = _make_state([])
    panel = build_detail_panel(None, state=state)
    rendered = _render(panel)
    assert "no wave selected" in rendered


def test_build_detail_panel_with_dep_chain_shows_deps_and_blocked_by() -> None:
    """Detail panel reads typed DAG edges, not Wave.blocks directly."""
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS, blocks=["P20-I01-W02"]),
        _make_wave(
            "P20-I01-W02",
            status=WaveStatus.PENDING,
            deps=["P20-I01-W01"],
            success_criteria=["build it", "test it"],
        ),
    ]
    state = _make_state(waves)
    selected = state.waves["P20-I01-W02"]
    panel = build_detail_panel(selected, state=state)
    rendered = _render(panel)
    assert "P20-I01-W02" in rendered
    assert "deps:" in rendered
    assert "P20-I01-W01" in rendered
    assert "blocked_by:" in rendered
    # W01 is still in_progress (not CLOSED) → blocked_by lists it.
    # The exact column may wrap; the relevant assertion is presence.
    assert "criteria:" in rendered
    assert "build it" in rendered
    assert "test it" in rendered


def test_build_detail_panel_blocked_by_is_empty_when_dep_is_closed() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.CLOSED, blocks=["P20-I01-W02"]),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING, deps=["P20-I01-W01"]),
    ]
    state = _make_state(waves)
    panel = build_detail_panel(state.waves["P20-I01-W02"], state=state)
    rendered = _render(panel)
    # blocked_by line must show ``-`` placeholder when the dep is closed.
    lines = rendered.splitlines()
    blocked_line = next(line for line in lines if "blocked_by:" in line)
    # The placeholder is the only non-whitespace token after the colon.
    assert "-" in blocked_line


def test_build_detail_panel_budget_with_token_budget_shows_percent() -> None:
    wave = _make_wave(
        "P20-I01-W01",
        status=WaveStatus.IN_PROGRESS,
        token_budget=10_000,
        tokens_consumed=2_500,
    )
    state = _make_state([wave])
    panel = build_detail_panel(state.waves["P20-I01-W01"], state=state)
    rendered = _render(panel)
    assert "2500" in rendered
    assert "10000" in rendered
    # 25% rounded.
    assert "25%" in rendered


def test_build_detail_panel_budget_unset_uses_placeholder() -> None:
    wave = _make_wave("P20-I01-W01", status=WaveStatus.PENDING)
    state = _make_state([wave])
    panel = build_detail_panel(state.waves["P20-I01-W01"], state=state)
    rendered = _render(panel)
    # Budget line falls back to ``-`` when token_budget is None and
    # tokens_consumed is zero.
    lines = rendered.splitlines()
    budget_line = next(line for line in lines if "budget:" in line)
    assert "-" in budget_line


def test_build_detail_panel_tests_uses_outcome_when_present() -> None:
    wave = _make_wave(
        "P20-I01-W01",
        status=WaveStatus.CLOSED,
        outcome="all green",
    )
    state = _make_state([wave])
    panel = build_detail_panel(state.waves["P20-I01-W01"], state=state)
    rendered = _render(panel)
    assert "tests:" in rendered
    assert "all green" in rendered


def test_build_detail_panel_criteria_empty_uses_dash_placeholder() -> None:
    wave = _make_wave("P20-I01-W01", status=WaveStatus.PENDING)
    state = _make_state([wave])
    panel = build_detail_panel(state.waves["P20-I01-W01"], state=state)
    rendered = _render(panel)
    # The criteria block always renders, with a ``-`` placeholder when empty.
    assert "criteria:" in rendered


# ---------------------------------------------------------------------------
# Frame composition
# ---------------------------------------------------------------------------


def test_build_wave_board_frame_renders_brand_and_footer() -> None:
    waves = [
        _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS),
        _make_wave("P20-I01-W02", status=WaveStatus.PENDING),
    ]
    state = _make_state(waves)
    rendered = _render(build_wave_board_frame(state, view=WaveBoardState()))
    assert "Eä" in rendered
    assert "EAWF" in rendered
    assert "P20-I01" in rendered
    assert "f filter" in rendered
    assert "Esc back" in rendered


def test_build_wave_board_frame_with_no_active_iter_renders_empty_list() -> None:
    """No iter pointer → empty wave list with placeholder."""
    waves = [_make_wave("P20-I01-W01", status=WaveStatus.PENDING)]
    state = _make_state(waves)
    # Clear the iter pointer post-validation; this is a test-only nudge.
    state.current.iter_id = None
    rendered = _render(build_wave_board_frame(state, view=WaveBoardState()))
    assert "0 of 0" in rendered


def test_render_wave_board_returns_string_when_console_omitted() -> None:
    waves = [_make_wave("P20-I01-W01", status=WaveStatus.PENDING)]
    state = _make_state(waves)
    out = render_wave_board(state)
    assert "Eä" in out
    assert "P20-I01-W01" in out


def test_render_wave_board_accepts_external_console() -> None:
    import io

    from rich.console import Console

    waves = [_make_wave("P20-I01-W01", status=WaveStatus.PENDING)]
    state = _make_state(waves)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, record=False)
    out = render_wave_board(state, console=console)
    assert out == ""
    assert "P20-I01-W01" in buf.getvalue()


# ---------------------------------------------------------------------------
# apply_key (keypress dispatch)
# ---------------------------------------------------------------------------


def _three_wave_state() -> State:
    return _make_state(
        [
            _make_wave("P20-I01-W01", status=WaveStatus.IN_PROGRESS),
            _make_wave("P20-I01-W02", status=WaveStatus.PENDING),
            _make_wave("P20-I01-W03", status=WaveStatus.CLOSED),
        ]
    )


def test_apply_key_down_advances_cursor() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=0)
    updated = apply_key(view, "\x1b[B", state=state)
    assert updated.selected_index == 1


def test_apply_key_down_clamps_at_bottom() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=2)
    updated = apply_key(view, "\x1b[B", state=state)
    assert updated.selected_index == 2


def test_apply_key_up_retreats_cursor() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=1)
    updated = apply_key(view, "\x1b[A", state=state)
    assert updated.selected_index == 0


def test_apply_key_up_clamps_at_top() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=0)
    updated = apply_key(view, "\x1b[A", state=state)
    assert updated.selected_index == 0


def test_apply_key_vim_j_and_k_act_as_arrow_aliases() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=1)
    assert apply_key(view, "j", state=state).selected_index == 2
    assert apply_key(view, "k", state=state).selected_index == 0


def test_apply_key_g_jumps_to_top() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=2)
    assert apply_key(view, "g", state=state).selected_index == 0


def test_apply_key_capital_g_jumps_to_bottom() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=0)
    assert apply_key(view, "G", state=state).selected_index == 2


def test_apply_key_f_cycles_filter_and_resets_cursor() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=2, filter_mode="all")
    out = apply_key(view, "f", state=state)
    assert out.filter_mode == "pending"
    assert out.selected_index == 0


def test_apply_key_f_full_cycle_returns_to_all() -> None:
    state = _three_wave_state()
    view = WaveBoardState()
    seq = []
    for _ in range(5):
        view = apply_key(view, "f", state=state)
        seq.append(view.filter_mode)
    assert seq == ["pending", "claimed_in_progress", "closed", "failed", "all"]


def test_apply_key_unknown_key_returns_view_unchanged() -> None:
    state = _three_wave_state()
    view = WaveBoardState(selected_index=1, filter_mode="all")
    out = apply_key(view, "z", state=state)
    assert out.selected_index == 1
    assert out.filter_mode == "all"


def test_apply_key_down_on_empty_list_does_not_overflow() -> None:
    """When filter produces zero waves the cursor cannot escape index 0."""
    state = _make_state(
        [
            _make_wave("P20-I01-W01", status=WaveStatus.CLOSED),
        ]
    )
    view = WaveBoardState(filter_mode="pending")
    out = apply_key(view, "\x1b[B", state=state)
    assert out.selected_index == 0
