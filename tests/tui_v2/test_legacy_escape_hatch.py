"""Tests for the C06 ``EAWF_TUI_LEGACY`` escape hatch + migration verdict.

The C06 migration verdict (closing the TUI band) is: bare ``eawf``
defaults to ``tui_v2``; the legacy ``src/eawf/tui/`` tree stays as a
parallel fallback for one alpha cycle, reachable via ``EAWF_TUI_LEGACY=1``;
deletion is deferred to a follow-up phase. These tests pin the
operative behaviour of that verdict at the dispatch boundary
(:func:`eawf.cli.app._dispatch_tui`) and assert the legacy tree is still
importable (not deleted).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import eawf.cli.app as cli_app

if TYPE_CHECKING:
    import pytest


def _stub_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    isatty: bool,
) -> dict[str, int]:
    """Stub the two launch targets + ``isatty``; return a call counter."""
    calls = {"tui_v2": 0, "legacy": 0}

    def fake_run_app(scope: str, state_path: object) -> int:
        calls["tui_v2"] += 1
        return 0

    def fake_run_tui(**_kwargs: object) -> int:
        calls["legacy"] += 1
        return 0

    monkeypatch.setattr("eawf.tui_v2.app.run_app", fake_run_app)
    monkeypatch.setattr("eawf.tui.app.run_tui", fake_run_tui)

    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return isatty

    monkeypatch.setattr("sys.stdout", _Stdout())
    return calls


# --------------------------------------------------------------------------
# Default (b): bare eawf on a TTY launches tui_v2
# --------------------------------------------------------------------------


def test_interactive_default_launches_tui_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAWF_TUI_LEGACY", raising=False)
    calls = _stub_dispatch(monkeypatch, isatty=True)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["tui_v2"] == 1
    assert calls["legacy"] == 0


# --------------------------------------------------------------------------
# Escape hatch (c): EAWF_TUI_LEGACY=1 routes the TTY path to legacy
# --------------------------------------------------------------------------


def test_legacy_env_routes_interactive_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAWF_TUI_LEGACY", "1")
    calls = _stub_dispatch(monkeypatch, isatty=True)
    rc = cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert rc == 0
    assert calls["legacy"] == 1
    assert calls["tui_v2"] == 0


def test_legacy_env_other_value_does_not_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the exact "1" opt-in flips to legacy; anything else is default.
    monkeypatch.setenv("EAWF_TUI_LEGACY", "true")
    calls = _stub_dispatch(monkeypatch, isatty=True)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert calls["tui_v2"] == 1
    assert calls["legacy"] == 0


# --------------------------------------------------------------------------
# Non-TTY / plain fallback uses the legacy deterministic renderer
# regardless of the flag (unchanged behaviour).
# --------------------------------------------------------------------------


def test_non_tty_falls_back_to_legacy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAWF_TUI_LEGACY", raising=False)
    calls = _stub_dispatch(monkeypatch, isatty=False)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=False)
    assert calls["legacy"] == 1
    assert calls["tui_v2"] == 0


def test_plain_flag_falls_back_to_legacy_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAWF_TUI_LEGACY", raising=False)
    calls = _stub_dispatch(monkeypatch, isatty=True)
    cli_app._dispatch_tui(workspace=None, no_input=False, plain=True)
    assert calls["legacy"] == 1
    assert calls["tui_v2"] == 0


# --------------------------------------------------------------------------
# Deferred deletion (d): the legacy tree is still importable.
# --------------------------------------------------------------------------


def test_legacy_tui_tree_not_deleted() -> None:
    legacy = importlib.import_module("eawf.tui")
    assert hasattr(legacy, "run_tui")
    # The legacy module documents its parallel-fallback status.
    assert legacy.__doc__ is not None
    assert "EAWF_TUI_LEGACY" in legacy.__doc__
