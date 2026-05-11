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
    _build_layout,
    _breadcrumb,
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
    assert counts == {
        "phases_open": 1,
        "iters_open": 1,
        "waves_pending": 1,
        "audits": 1,
    }


def test_build_status_text_carries_brand_and_keymap() -> None:
    text = build_status_text(_state())
    assert text.startswith("Eä")
    assert "↑↓ navigate" in text
    assert "waves_pending=1" in text


def test_render_layout_writes_into_buffer() -> None:
    output = render_layout(_state())
    assert "Eä" in output
    assert "DEMO" in output
    assert "↑↓ navigate" in output


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
