"""MODES chassis for the Eae Textual TUI.

The TUI runs on Textual's native :attr:`textual.app.App.MODES` +
``switch_mode``: a **mode** is a content surface (Home / Trust / Doctor /
Evidence / Feed / Config) switched with digit keys ``1``..``6``, each
owning an independent screen stack. :mod:`eawf.surfaces.tui.modes.registry`
is the single seam the mode set is declared in, so the per-pane waves add
their mode with one registration line (the recipe in the registry module
docstring) instead of editing a central dict in ``app.py``.

Mode and scope are orthogonal: the scope switch (``w`` / ``r`` / ``u``)
stays an in-mode operation -- the Home mode renders the resolved scope
screen.
"""

from __future__ import annotations

from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
from eawf.surfaces.tui.modes.feed import FeedModeScreen
from eawf.surfaces.tui.modes.placeholder import PlaceholderModeScreen
from eawf.surfaces.tui.modes.registry import (
    DEFAULT_MODE,
    MODE_REGISTRY,
    ModeSpec,
    build_modes,
    mode_bindings,
    mode_for_name,
    mode_title,
)

__all__ = [
    "DEFAULT_MODE",
    "MODE_REGISTRY",
    "EvidenceModeScreen",
    "FeedModeScreen",
    "ModeSpec",
    "PlaceholderModeScreen",
    "build_modes",
    "mode_bindings",
    "mode_for_name",
    "mode_title",
]
