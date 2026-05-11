"""Eä TUI scaffold (P14-W10 / D15 + D23).

Public re-exports:

    render_layout(state, *, console=None) -> str
    run_tui(workspace=None) -> int
"""

from __future__ import annotations

from eawf.tui.app import build_status_text, render_layout, run_tui

__all__ = [
    "build_status_text",
    "render_layout",
    "run_tui",
]
