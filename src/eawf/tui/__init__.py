"""Eä TUI scaffold (P14-W10 / D15 + D23).

Public re-exports:

    render_layout(state, *, console=None) -> str
    run_tui(workspace=None) -> int

Legacy-status note (C06 migration). This is the **legacy** TUI tree. The
v0.3+ operator surface is the parallel :mod:`eawf.tui_v2` Textual tree,
which is now the bare-``eawf`` default. This legacy tree stays as a
working fallback for one alpha cycle and is reachable via the
``EAWF_TUI_LEGACY=1`` escape hatch; its deletion is deferred to a
follow-up phase. The authoritative migration verdict lives in
:mod:`eawf.tui_v2.snapshot`.
"""

from __future__ import annotations

from eawf.tui.app import build_status_text, render_layout, run_tui

__all__ = [
    "build_status_text",
    "render_layout",
    "run_tui",
]
