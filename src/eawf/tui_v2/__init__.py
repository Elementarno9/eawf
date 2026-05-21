"""Eä Textual TUI (tui_v2) — operator-surface rebuild.

The v0.3+ TUI is built on Textual (reversing the prior ``rich``
pick); this package is the operator-surface rebuild that replaced the
now-removed legacy ``eawf.tui`` package.

Public re-exports:

    EaApp(scope, state_path) -> App[None]
    run_app(scope, state_path) -> int
"""

from __future__ import annotations

from eawf.tui_v2.app import EaApp, run_app

__all__ = [
    "EaApp",
    "run_app",
]
