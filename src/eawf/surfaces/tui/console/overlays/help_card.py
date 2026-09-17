"""The help overlay: this route's keymap, from the keybar's own table, then the global keys."""

from __future__ import annotations

from eawf.surfaces.tui.console.frame import View, bar, build, entry_state, header, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import ENTRY_ALLOW, ENTRY_ROUTE, GLOBAL_HELP, route_keys
from eawf.surfaces.tui.console.width import cell_len, pad

# The token column's width; a longer token widens its own row rather than being cut.
TOKEN_W = 11


def _token(token: str) -> str:
    return pad(token, max(TOKEN_W, cell_len(token) + 2))


def render(view: View) -> list[str]:
    """Return the help overlay."""
    s, fx, w = view.session, view.fixture, view.w
    rows = [
        header(view, f" Eä ▸ help · {s.route}"),
        " the keymap for THIS route, not a global cheat sheet",
        bar(w),
        " THIS ROUTE",
    ]
    table = route_keys(s, fx)
    rows.extend("   " + _token(e.token) + e.label for e in table)
    if not table:
        rows.append("   no route-local key — only the global set below applies")
    pre = s.route == ENTRY_ROUTE
    allowed = {key for key, _label in entry_state(view).keys} | set(ENTRY_ALLOW)
    rows.extend([thin(w), " EVERYWHERE"])
    rows.extend(
        "   " + _token(token) + text
        for token, text, key in GLOBAL_HELP
        if not pre or key in allowed
    )
    if pre:
        rows.append("   before a session exists, only the keys above are bound")
    rows.extend(
        [
            thin(w),
            " Esc Esc quits only at scope home, with nothing open and no",
            " outstanding control — and only if the presses are 80ms to 1.5s apart.",
        ]
    )
    return build(view, rows, keybar([("Esc", "close")], w))
