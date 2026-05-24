"""Tests for the bare-``eawf`` / ``eawf tui`` dispatch after legacy removal.

The prior Rich-based TUI (and its ``EAWF_TUI_LEGACY=1`` escape hatch)
has been removed — ``tui`` is the only TUI surface. These tests pin
the dispatch contract at the boundary
(:func:`eawf.surfaces.cli.app._dispatch_tui`):

* an interactive TTY launches the Textual :class:`~eawf.surfaces.tui.app.EaApp`;
* the non-TTY / ``--plain`` / ``--no-input`` path emits the deterministic
  ``tui`` status frame (:func:`eawf.surfaces.tui.offline.emit_status`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

import eawf.surfaces.cli.app as cli_app
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.brand import BRAND_LITERAL

if TYPE_CHECKING:
    pass


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    isatty: bool,
) -> dict[str, int]:
    """Stub the interactive launch + status emitter; return a call counter."""
    calls = {"tui": 0, "status": 0}

    def fake_run_app(scope: str, state_path: object) -> int:
        calls["tui"] += 1
        return 0

    def fake_emit_status(**_kwargs: object) -> int:
        calls["status"] += 1
        return 0

    monkeypatch.setattr("eawf.surfaces.tui.app.run_app", fake_run_app)
    monkeypatch.setattr("eawf.surfaces.tui.offline.emit_status", fake_emit_status)

    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return isatty

    monkeypatch.setattr("sys.stdout", _Stdout())
    return calls


# --------------------------------------------------------------------------
# Interactive TTY launches tui (no escape hatch remains).
# --------------------------------------------------------------------------


def test_interactive_launches_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["tui"] == 1
    assert calls["status"] == 0


def test_legacy_env_no_longer_routes_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EAWF_TUI_LEGACY=1`` is dead — the TTY path still launches tui."""
    monkeypatch.setenv("EAWF_TUI_LEGACY", "1")
    calls = _stub_dispatch(monkeypatch, isatty=True)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["tui"] == 1
    assert calls["status"] == 0


# --------------------------------------------------------------------------
# Non-TTY / plain / no-input fall back to the tui status emitter.
# --------------------------------------------------------------------------


def test_non_tty_falls_back_to_status_emitter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=False)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert calls["status"] == 1
    assert calls["tui"] == 0


def test_plain_flag_falls_back_to_status_emitter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=True)
    assert calls["status"] == 1
    assert calls["tui"] == 0


def test_no_input_flag_falls_back_to_status_emitter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True)
    cli_app._dispatch_tui(workspace=None, no_input=True, plain=False)
    assert calls["status"] == 1
    assert calls["tui"] == 0


# --------------------------------------------------------------------------
# CLI-level non-TTY contract (carried over from the removed legacy tests):
# bare eawf / eawf tui emit the deterministic status frame, exit 0.
# --------------------------------------------------------------------------


def test_bare_cli_non_tty_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--plain", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert BRAND_LITERAL in result.stdout
    assert "keymap:" in result.stdout


def test_tui_subcommand_non_tty_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["-w", str(tmp_path), "--plain", "tui"])
    assert result.exit_code == 0
    assert BRAND_LITERAL in result.stdout
    assert "keymap:" in result.stdout


def test_bare_cli_no_input_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--no-input", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert BRAND_LITERAL in result.stdout
