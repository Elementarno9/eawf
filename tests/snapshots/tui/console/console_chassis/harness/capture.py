"""Capture the screen exactly as the compositor painted it: one row per Strip, trailing
spaces kept, no ANSI, never right-stripped and never trimmed of blank rows."""

from __future__ import annotations

from textual.app import App


def capture_rows(app: App) -> list[str]:
    strips = app.screen._compositor.render_strips()
    return [strip.text for strip in strips]


def capture(app: App) -> str:
    return "\n".join(capture_rows(app))


def capture_cells(app: App) -> list[int]:
    """Per-row cell widths, which is the width check (never ``len``)."""
    return [strip.cell_length for strip in app.screen._compositor.render_strips()]
