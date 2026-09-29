"""The help overlay: this route's keymap, from the keybar's own table, then the global keys.

It teaches only keys that act where the operator stands: the route's own table, and each
global key that acts on this route, so a depth key is taught only where there is a depth.
"""

from __future__ import annotations

from eawf.surfaces.tui.console.frame import View, bar, build, entry_state, header, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, KeyEntry, keybar
from eawf.surfaces.tui.console.keymap import (
    ENTRY_ALLOW,
    ENTRY_ROUTE,
    GLOBAL_KEYS,
    acts_here,
    native_keys,
    route_keys,
)
from eawf.surfaces.tui.console.width import cell_len, pad

# The token column's narrowest width; the card's longest token widens the whole column.
TOKEN_W = 11


def _route_table(view: View) -> tuple[KeyEntry, ...]:
    """Return the keys the frame under the overlay advertises.

    A frame drawn from a held read model lists the native table its keybar draws from
    rather than the prototype table it replaced, and like the bar it teaches paging only
    while a table of the frame is cut to its window, since nothing else pages.
    """
    s = view.session
    native = view.projection is not None or view.register is not None
    if native and s.route != ENTRY_ROUTE and s.route in ROUTE_KEYS:
        return native_keys(s.route, windowed=s.route_windowed)
    return route_keys(s, view.fixture)


def render(view: View) -> list[str]:
    """Return the help overlay."""
    s, w = view.session, view.w
    rows = [
        header(view, f" Eä ▸ help · {s.route}"),
        " the keymap for THIS route, not a global cheat sheet",
        bar(w),
        " THIS ROUTE",
    ]
    table = _route_table(view)
    pre = s.route == ENTRY_ROUTE
    allowed = {key for key, _label in entry_state(view).keys} | set(ENTRY_ALLOW)
    everywhere = [
        key
        for key in GLOBAL_KEYS
        if acts_here(key, s.route, linked=view.linked) and (not pre or allowed.issuperset(key.keys))
    ]
    tokens = [e.token for e in table] + [key.token for key in everywhere]
    card_w = max([TOKEN_W, *(cell_len(token) + 2 for token in tokens)])

    native = view.projection is not None or view.register is not None

    def key_cell(token: str) -> str:
        # a card over a held read model keeps one key column, so every meaning starts in
        # the same cell; the prototype registers replay the pack's recording, which widens
        # a long key's own row
        return pad(token, card_w if native else max(TOKEN_W, cell_len(token) + 2))

    rows.extend("   " + key_cell(e.token) + e.label for e in table)
    if not table:
        rows.append("   no route-local key — only the global set below applies")
    rows.extend([thin(w), " EVERYWHERE"])
    rows.extend("   " + key_cell(key.token) + key.text for key in everywhere)
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
