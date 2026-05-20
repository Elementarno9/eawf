"""Eä Textual TUI (tui_v2) — operator-surface rebuild.

The v0.3+ TUI is built on Textual (reversing the prior ``rich``
pick); this package is the parallel rebuild that supersedes
:mod:`eawf.tui` once its cutover is ratified in a later wave.

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
