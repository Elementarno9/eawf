"""Full-screen + overlay screens for the Eä TUI (tui).

The non-scope screens of the TUI: the :class:`HelpScreen` full-keymap
overlay (this wave) and the modal overlays under
:mod:`eawf.tui.screens.overlays`. The per-scope screens
(``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``) live in
:mod:`eawf.tui.scopes`; this package holds the screens that overlay
*on top of* a scope screen (help, detail, confirm, and the audit /
plan-preview / metrics overlays that land in later waves of this band).
"""

from __future__ import annotations

from eawf.tui.screens.help import HelpScreen, open_help

__all__ = [
    "HelpScreen",
    "open_help",
]
