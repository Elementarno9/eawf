"""Smoke tests for the Eä TUI scaffold (P14-W10 / D15 + D23)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.tui.app import (
    _breadcrumb,
    _build_layout,
    _summary_counts,
    build_status_text,
    render_layout,
)


def _state(**overrides: Any) -> dict[str, Any]:
    base = {
        "project": {"code": "DEMO"},
        "current": {"phase_id": "P03", "iter_id": "P03-I01"},
        "phases": {"P03": {"status": "active"}},
        "iters": {"P03-I01": {"status": "active"}},
        "waves": {"P03-I01-W01": {"status": "pending"}},
        "audits": {"A01": {"status": "complete"}},
    }
    base.update(overrides)
    return base


def test_breadcrumb_includes_project_phase_iter() -> None:
    crumbs = _breadcrumb(_state())
    assert crumbs == "DEMO / P03 / P03-I01"


def test_breadcrumb_handles_minimal_state() -> None:
    assert _breadcrumb({}) == "EAWF"


def test_summary_counts_extract_open_pending_audits() -> None:
    counts = _summary_counts(_state())
    # P20-I01-W02 expanded the counter set to include in-progress waves
    # for the quadrant roadmap pane; P20-I03-W01 added ``iters_closed``
    # for the new roadmap line. Sample state has neither.
    assert counts == {
        "phases_open": 1,
        "iters_open": 1,
        "iters_closed": 0,
        "waves_pending": 1,
        "waves_in_progress": 0,
        "audits": 1,
    }


def test_build_status_text_carries_brand_and_keymap() -> None:
    text = build_status_text(_state())
    assert text.startswith("Eä")
    # P20-I03-W01 rewrote the quadrant keymap to advertise quadrant-
    # level keys (board / config / overlay / quit) instead of the
    # wave-board navigation hint.
    assert "b board" in text
    assert "overlay" in text
    assert "waves_pending=1" in text


def test_render_layout_writes_into_buffer() -> None:
    output = render_layout(_state())
    assert "Eä" in output
    assert "DEMO" in output
    # P20-I03-W01 quadrant keymap fragments — board entry + overlay
    # verb-prefix advertisement.
    assert "board" in output
    assert "overlay" in output
    assert "Esc" in output


def test_render_layout_accepts_external_console() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    render_layout(_state(), console=console)
    assert "Eä" in buf.getvalue()


def test_layout_has_three_rows() -> None:
    layout = _build_layout(_state())
    children = list(layout.children)
    assert {c.name for c in children} == {"header", "body", "footer"}


def test_bare_eawf_non_tty_emits_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--plain", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "Eä" in result.stdout
    assert "keymap:" in result.stdout


def test_eawf_tui_subcommand_non_tty_returns_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["-w", str(tmp_path), "--plain", "tui"])
    assert result.exit_code == 0
    assert "Eä" in result.stdout


def test_bare_eawf_no_input_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--no-input", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "Eä" in result.stdout


def test_run_tui_exits_on_esc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: "\x1b")
    assert rc == 0


def test_run_tui_exits_on_q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: "q")
    assert rc == 0


def test_run_tui_exits_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)

    def boom() -> str:
        raise KeyboardInterrupt

    rc = tui_app.run_tui(workspace=tmp_path, read_key=boom)
    assert rc == 0


def test_run_tui_loop_iterates_until_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # P20-W03 reserved ``b`` for "open wave-board view"; P20-W11
    # reserved ``c`` for "open config modal". The loop test picks
    # neutral inert keys (``a``/``z``) so the exit semantics stay the
    # assertion target.
    keys = iter(["a", "z", "\x1b"])

    def feeder() -> str:
        return next(keys)

    rc = tui_app.run_tui(workspace=tmp_path, read_key=feeder)
    assert rc == 0


def test_run_tui_exits_on_eof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: "")
    assert rc == 0


def test_run_tui_does_not_exit_on_arrow_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrow keys arrive as ESC-prefixed sequences; bare-ESC exit must not fire."""
    from eawf.tui import app as tui_app

    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    keys = iter(["\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "q"])

    def feeder() -> str:
        return next(keys)

    rc = tui_app.run_tui(workspace=tmp_path, read_key=feeder)
    assert rc == 0
    with pytest.raises(StopIteration):
        next(keys)
