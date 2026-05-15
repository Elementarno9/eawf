"""Integration tests for the wave-board TUI view (P20-I01-W03).

Covers:

* Golden snapshots of the offline render across multiple views
  (default selection, mid-list selection, filter=pending,
  filter=closed). Fixture state is checked in beside this test
  (``tests/golden/tui/wave_board_state.json``).
* The ``b``-key dispatch in :func:`eawf.tui.app.run_tui`: pressing
  ``b`` opens the wave-board sub-loop; Esc returns to the quadrant.
* Filter-cycle wiring: pressing ``f`` advances the filter mode and
  re-renders.

When the renderer drifts intentionally, regenerate the snapshots:

    cd <repo>
    uv run python -c "
    import io, json
    from pathlib import Path
    from rich.console import Console
    from eawf.state.models import State
    from eawf.tui.wave_board import build_wave_board_frame, WaveBoardState

    fixture = Path('tests/golden/tui')
    state = State.model_validate(
        json.loads((fixture / 'wave_board_state.json').read_text())
    )
    for view, name in [
        (WaveBoardState(), 'wave_board_default.txt'),
        (WaveBoardState(selected_index=2), 'wave_board_failed_selected.txt'),
        (WaveBoardState(filter_mode='pending'), 'wave_board_filter_pending.txt'),
        (WaveBoardState(filter_mode='closed'), 'wave_board_filter_closed.txt'),
    ]:
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=100, height=30, record=False).print(
            build_wave_board_frame(state, view=view)
        )
        (fixture / name).write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from eawf.state.models import State
from eawf.tui import app as tui_app
from eawf.tui.wave_board import (
    WAVE_BOARD_FOOTER,
    WaveBoardState,
    build_wave_board_frame,
)

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_state() -> State:
    payload = json.loads((_FIXTURE_DIR / "wave_board_state.json").read_text(encoding="utf-8"))
    return State.model_validate(payload)


def _render(state: State, view: WaveBoardState) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, height=30, record=False)
    console.print(build_wave_board_frame(state, view=view))
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text[:-1]
    return text


# ---------------------------------------------------------------------------
# Golden snapshots
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_wave_board_default_view_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(state, WaveBoardState()))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "wave_board_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "wave-board default-view drift — regenerate "
        "tests/golden/tui/wave_board_default.txt with the snippet at the top "
        "of test_tui_wave_board.py."
    )


@pytest.mark.golden
def test_wave_board_failed_selected_view_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(state, WaveBoardState(selected_index=2)))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "wave_board_failed_selected.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_wave_board_filter_pending_view_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(state, WaveBoardState(filter_mode="pending")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "wave_board_filter_pending.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_wave_board_filter_closed_view_matches_golden() -> None:
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render(state, WaveBoardState(filter_mode="closed")))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "wave_board_filter_closed.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions (independent of byte-equality)
# ---------------------------------------------------------------------------


def test_wave_board_renders_brand_outside_left_of_breadcrumb() -> None:
    state = _load_fixture_state()
    rendered = _render(state, WaveBoardState())
    brand_idx = rendered.find("Eä")
    crumb_idx = rendered.find("EAWF")
    assert brand_idx >= 0 and crumb_idx > brand_idx


def test_wave_board_lists_every_wave_in_priority_order() -> None:
    state = _load_fixture_state()
    rendered = _render(state, WaveBoardState())
    # in_progress (W03) > pending (W04) > failed (W05) > closed (W01,W02 by id).
    expected_order = ["P20-I01-W03", "P20-I01-W04", "P20-I01-W05", "P20-I01-W01", "P20-I01-W02"]
    indices = [rendered.find(wid) for wid in expected_order]
    assert all(i >= 0 for i in indices)
    assert indices == sorted(indices), "wave list out of priority order"


def test_wave_board_detail_panel_uses_typed_dag_edges() -> None:
    """Selecting a pending wave with an in-progress dep shows blocked_by."""
    state = _load_fixture_state()
    # W04 is pending and deps=[W03 in_progress] → blocked_by lists W03.
    rendered = _render(state, WaveBoardState(filter_mode="pending"))
    assert "P20-I01-W04" in rendered
    assert "blocked_by:" in rendered
    # The blocked_by line must mention W03 (the open dep).
    lines = rendered.splitlines()
    blocked_lines = [line for line in lines if "blocked_by:" in line]
    assert len(blocked_lines) == 1
    assert "P20-I01-W03" in blocked_lines[0]


def test_wave_board_footer_carries_filter_and_back_keys() -> None:
    state = _load_fixture_state()
    rendered = _render(state, WaveBoardState())
    assert "f filter" in rendered
    assert "Esc back" in rendered
    # The wave-board footer is distinct from the quadrant footer.
    assert WAVE_BOARD_FOOTER.split("(")[0].strip().split()[-1] in rendered


# ---------------------------------------------------------------------------
# Key dispatch in run_tui (b opens wave-board, Esc returns)
# ---------------------------------------------------------------------------


def test_run_tui_opens_wave_board_on_b_and_exits_on_double_esc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``b`` enters wave-board; Esc returns; second Esc exits."""
    # Seed the workspace with the fixture state so the wave-board has
    # real waves to render.
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    shutil.copy(_FIXTURE_DIR / "wave_board_state.json", state_dir / "state.json")

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # b → open board; \x1b → back to quadrant; \x1b → exit.
    keys = iter(["b", "\x1b", "\x1b"])
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0
    # The iterator must be fully consumed.
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_wave_board_filter_key_cycles_then_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In wave-board view, ``f`` advances the filter without exiting."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    shutil.copy(_FIXTURE_DIR / "wave_board_state.json", state_dir / "state.json")

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # b open → f cycle → f cycle → \x1b back → q exit.
    keys = iter(["b", "f", "f", "\x1b", "q"])
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_wave_board_arrow_key_does_not_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In wave-board view, arrow keys (CSI sequences) must not be treated as Esc."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    shutil.copy(_FIXTURE_DIR / "wave_board_state.json", state_dir / "state.json")

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # b open → down arrow → up arrow → \x1b back → q exit.
    keys = iter(["b", "\x1b[B", "\x1b[A", "\x1b", "q"])
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_wave_board_b_key_when_state_missing_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No state.json present → pressing ``b`` opens an empty-plan placeholder.

    The wave-board sub-loop must not crash when the workspace has no
    state file. The render degrades to the quadrant frame and the
    operator can press Esc to return.
    """
    # Intentionally do NOT seed .ea/state.json.
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    keys = iter(["b", "\x1b", "q"])
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0


# ---------------------------------------------------------------------------
# Two-render byte stability (no nondeterministic ordering)
# ---------------------------------------------------------------------------


def test_wave_board_two_renders_byte_stable() -> None:
    state = _load_fixture_state()
    first = _render(state, WaveBoardState())
    second = _render(state, WaveBoardState())
    assert first == second
