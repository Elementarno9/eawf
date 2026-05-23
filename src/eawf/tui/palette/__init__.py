"""The ``/`` command-palette package for the Eä TUI (tui).

Re-exports the palette overlay (:class:`CommandPalette` +
:func:`open_palette`) and the static verb registry surface
(:class:`PaletteVerb`, :data:`VERBS`, :func:`visible_verbs`,
:func:`rank_verbs`) so host screens import from one place
(``eawf.tui.palette``) rather than reaching into the submodules.
"""

from __future__ import annotations

from eawf.tui.palette.command_palette import (
    PALETTE_PREFIX,
    CommandPalette,
    open_palette,
)
from eawf.tui.palette.verbs import (
    SCOPES_ALL,
    VERBS,
    PaletteVerb,
    ScopeName,
    VerbHandler,
    fuzzy_score,
    rank_verbs,
    split_verb_args,
    visible_verbs,
)

__all__ = [
    "PALETTE_PREFIX",
    "SCOPES_ALL",
    "VERBS",
    "CommandPalette",
    "PaletteVerb",
    "ScopeName",
    "VerbHandler",
    "fuzzy_score",
    "open_palette",
    "rank_verbs",
    "split_verb_args",
    "visible_verbs",
]
