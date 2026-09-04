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
from eawf.surfaces.render.brand import render_wordmark_ansi

if TYPE_CHECKING:
    pass

#: The two-tone green brand wordmark the non-TTY status frame now heads with.
#: Asserting the full wordmark (not the bare ``Eä`` literal) keeps the dispatch
#: contract in lockstep with the W32 offline reskin -- the bare literal is no
#: longer contiguous because the ANSI accent escape sits between the ``E`` and
#: the ``ä``.
_WORDMARK = render_wordmark_ansi()


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    isatty: bool,
    state_root: Path,
) -> dict[str, int]:
    """Stub the interactive launch + status emitter; return a call counter.

    ``_dispatch_tui(workspace=None)`` resolves the active state by
    pwd-upward walk, which from the repo root lands on the project's own
    ``.ea/state.json``. ``state_root`` points the resolver at an empty tmp
    tree so the dispatch contract is exercised against no state at all.
    """
    calls = {"tui": 0, "status": 0}
    monkeypatch.setenv("EA_STATE", str(state_root / ".ea" / "state.json"))

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


def test_interactive_launches_tui(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True, state_root=tmp_path)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["tui"] == 1
    assert calls["status"] == 0


def test_legacy_env_no_longer_routes_anywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``EAWF_TUI_LEGACY=1`` is dead — the TTY path still launches tui."""
    monkeypatch.setenv("EAWF_TUI_LEGACY", "1")
    calls = _stub_dispatch(monkeypatch, isatty=True, state_root=tmp_path)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["tui"] == 1
    assert calls["status"] == 0


# --------------------------------------------------------------------------
# Non-TTY / plain / no-input fall back to the tui status emitter.
# --------------------------------------------------------------------------


def test_non_tty_falls_back_to_status_emitter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=False, state_root=tmp_path)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert calls["status"] == 1
    assert calls["tui"] == 0


def test_plain_flag_falls_back_to_status_emitter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True, state_root=tmp_path)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=True)
    assert calls["status"] == 1
    assert calls["tui"] == 0


def test_no_input_flag_falls_back_to_status_emitter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_dispatch(monkeypatch, isatty=True, state_root=tmp_path)
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
    assert _WORDMARK in result.stdout
    assert "keymap:" in result.stdout


def test_tui_subcommand_non_tty_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["-w", str(tmp_path), "--plain", "tui"])
    assert result.exit_code == 0
    assert _WORDMARK in result.stdout
    assert "keymap:" in result.stdout


def test_bare_cli_no_input_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--no-input", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert _WORDMARK in result.stdout
