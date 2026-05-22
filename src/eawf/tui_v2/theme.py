"""Per-theme palette definitions for the Eä TUI rebuild (tui_v2).

The runtime ``/theme`` swap rebinds the semantic colour vars
(``$accent`` / ``$primary`` / ``$ok`` / ``$warn`` / ``$err`` / ``$muted``
and the ``$status-*`` lifecycle tints) at the App level. Those vars used
to live at global scope in ``theme.tcss``; a global definition cannot
change at runtime, so the swap would recolour nothing. Hosting the vars
inside each :class:`~textual.theme.Theme`'s ``variables`` map instead
lets :meth:`textual.app.App.get_css_variables` re-resolve every ``$var``
the structural CSS references when the active theme changes — the swap
becomes a pure var rebind, exactly as the structural CSS was written to
expect.

Every theme the App can switch to MUST carry the full semantic var set,
otherwise the structural CSS in ``theme.tcss`` references an undefined
var on that theme. The four operator-facing logical names map onto
registered Textual theme names through :data:`LOGICAL_THEMES`:

* ``dark`` → :data:`EA_DARK` — the Wong 2011 deuteranopia-safe palette,
  carrying the exact hex values that shipped at global scope in
  ``theme.tcss`` (so selecting ``dark`` is a no-op visual baseline; the
  migration introduces no regression).
* ``cb`` → :data:`EA_CB` — the IBM colour-blind-safe palette, visually
  distinct from Wong so a second swap is observable.
* ``light`` → :data:`EA_LIGHT` — a light-background variant that carries
  the same semantic var *names* tuned for a light surface, built on
  Textual's ``textual-light`` base colours.
* ``auto`` → terminal-background detect, resolving to ``dark`` or
  ``light``. The synchronous swap path has no terminal-background query
  available, so ``auto`` resolves to the dark baseline; the async OSC
  probe that would refine it is out of scope here.
"""

from __future__ import annotations

import logging
from typing import Final

from textual.theme import Theme

logger = logging.getLogger(__name__)

#: Wong 2011 deuteranopia-safe semantic vars — the exact hex values that
#: shipped at global scope in ``theme.tcss`` before the per-theme
#: migration. Hosted here so selecting ``dark`` reproduces the original
#: look byte-for-byte (no visual regression).
_WONG_VARIABLES: Final[dict[str, str]] = {
    "accent": "#56b6c2",
    "primary": "#56b6c2",
    "ok": "#009e73",
    "warn": "#e69f00",
    "err": "#d55e00",
    "muted": "#6c6c6c",
    "status-pending": "#6c6c6c",
    "status-claimed": "#56b6c2",
    "status-in-progress": "#e69f00",
    "status-closed": "#009e73",
    "status-failed": "#d55e00",
}

#: IBM colour-blind-safe semantic vars — a palette visually distinct from
#: Wong (bluer accent, magenta error, gold in-progress) so a swap away
#: from ``dark`` is observable while staying colour-blind-safe.
_IBM_VARIABLES: Final[dict[str, str]] = {
    "accent": "#648fff",
    "primary": "#648fff",
    "ok": "#1a9988",
    "warn": "#ffb000",
    "err": "#dc267f",
    "muted": "#8a8a8a",
    "status-pending": "#8a8a8a",
    "status-claimed": "#648fff",
    "status-in-progress": "#ffb000",
    "status-closed": "#1a9988",
    "status-failed": "#dc267f",
}

#: Light-surface semantic vars — the same var *names* the structural CSS
#: references, retuned so the tints stay legible on a light background.
_LIGHT_VARIABLES: Final[dict[str, str]] = {
    "accent": "#007a87",
    "primary": "#007a87",
    "ok": "#007a52",
    "warn": "#a35b00",
    "err": "#a8331a",
    "muted": "#595959",
    "status-pending": "#595959",
    "status-claimed": "#007a87",
    "status-in-progress": "#a35b00",
    "status-closed": "#007a52",
    "status-failed": "#a8331a",
}


#: The Wong deuteranopia-safe dark theme — the default + the ``dark``
#: logical name. Its ``variables`` carry the exact pre-migration hex.
EA_DARK: Final[Theme] = Theme(
    name="ea-dark",
    primary="#56b6c2",
    accent="#56b6c2",
    success="#009e73",
    warning="#e69f00",
    error="#d55e00",
    dark=True,
    variables=dict(_WONG_VARIABLES),
)

#: The IBM colour-blind-safe dark theme — the ``cb`` logical name.
EA_CB: Final[Theme] = Theme(
    name="ea-cb",
    primary="#648fff",
    accent="#648fff",
    success="#1a9988",
    warning="#ffb000",
    error="#dc267f",
    dark=True,
    variables=dict(_IBM_VARIABLES),
)

#: The light-surface theme — the ``light`` logical name. Carries the
#: semantic var set retuned for a light background so the structural CSS
#: keeps resolving every ``$var`` it references.
EA_LIGHT: Final[Theme] = Theme(
    name="ea-light",
    primary="#007a87",
    accent="#007a87",
    success="#007a52",
    warning="#a35b00",
    error="#a8331a",
    dark=False,
    variables=dict(_LIGHT_VARIABLES),
)


#: Every custom theme the App registers. Order is registration order.
EA_THEMES: Final[tuple[Theme, ...]] = (EA_DARK, EA_CB, EA_LIGHT)

#: The default logical theme applied on startup when ``ui.theme`` is unset
#: — the Wong dark baseline, so a fresh launch matches the pre-migration
#: look.
DEFAULT_THEME: Final[str] = "dark"

#: Operator-facing logical names → registered Textual theme name. The
#: four logical names are the ``/theme`` argument grammar and the
#: ``ui.theme`` config choices; ``auto`` is resolved separately by
#: :func:`resolve_theme_name` since it depends on terminal background.
LOGICAL_THEMES: Final[dict[str, str]] = {
    "dark": EA_DARK.name,
    "cb": EA_CB.name,
    "light": EA_LIGHT.name,
}

#: The four logical names the operator may pass to ``/theme`` / persist in
#: ``ui.theme``. Kept as a tuple so the config registry choices and the
#: verb's accepted set share one source.
THEME_CHOICES: Final[tuple[str, ...]] = ("dark", "light", "cb", "auto")


def resolve_theme_name(logical: str) -> str | None:
    """Resolve an operator-facing logical name to a registered theme name.

    ``auto`` resolves to the dark baseline: the synchronous swap path has
    no terminal-background query available, so the honest best-effort
    result is the default dark theme. Any other unknown name returns
    ``None`` so the caller can reject it without changing the theme.

    Args:
        logical: One of ``"dark"`` / ``"light"`` / ``"cb"`` / ``"auto"``.

    Returns:
        The registered Textual theme name, or ``None`` when *logical* is
        not a recognised logical name.
    """
    if logical == "auto":
        return LOGICAL_THEMES[DEFAULT_THEME]
    return LOGICAL_THEMES.get(logical)


__all__ = [
    "DEFAULT_THEME",
    "EA_CB",
    "EA_DARK",
    "EA_LIGHT",
    "EA_THEMES",
    "LOGICAL_THEMES",
    "THEME_CHOICES",
    "resolve_theme_name",
]
