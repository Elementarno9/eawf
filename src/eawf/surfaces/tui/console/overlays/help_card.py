"""The help overlay: this route's keymap, from the keybar's own table, then the global keys.

It teaches only keys that act where the operator stands: the route's own table, and each
global key that acts on this route, so a depth key is taught only where there is a depth.
The table is never cut to fit: a card taller than the frame keeps its title rows and
scrolls the rest under a ``WINDOW`` row, so every key and the quit rule stay reachable.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from eawf.surfaces.tui.console.frame import (
    RowWindow,
    View,
    bar,
    build,
    entry_state,
    header,
    thin,
)
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, KeyEntry, keybar
from eawf.surfaces.tui.console.keymap import (
    ENTRY_ALLOW,
    ENTRY_ROUTE,
    GLOBAL_KEYS,
    acts_here,
    native_keys,
    route_keys,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.width import cell_len, pad

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.navigation import Ctx

# The token column's narrowest width; the card's longest token widens the whole column.
TOKEN_W = 11
# The title, subtitle and rule the card keeps above its scrolling table.
_TITLE_ROWS = 3
_SCROLL_KEYS = ("PageUp", "PageDown")
QUIT_RULE = "Esc Esc quits only at scope home, nothing open or pending, 80ms to 1.5s apart."


def _route_table(view: View) -> tuple[KeyEntry, ...]:
    """Return the keys the frame under the overlay advertises.

    A frame drawn from a held read model lists the native table its keybar draws from
    rather than the prototype table it replaced, and only the keys that bar offered: a key
    the frame gave up because it has nothing to act on -- paging on an uncut table, Tab
    with no second region, a copy with nothing to copy -- is not taught either.
    """
    s = view.session
    native = view.projection is not None or view.register is not None
    if native and s.route != ENTRY_ROUTE and s.route in ROUTE_KEYS:
        table = native_keys(s.route, windowed=s.route_windowed)
        offered = s.route_bar_keys
        if offered is None:
            return table
        return tuple(entry for entry in table if offered.intersection(entry.keys))
    return route_keys(s, view.fixture)


def _route_named(view: View) -> str:
    """Return how the crumb names the route under the card: by its word on a linked console.

    The prototype replay keeps the route id its recorded crumb names.
    """
    route = view.session.route
    return route if view.fixture.prototype else REGISTRY.route_word(route)


def render(view: View) -> list[str]:
    """Return the help overlay."""
    s, w = view.session, view.w
    rows = [
        header(view, f" Eä ▸ help · {_route_named(view)}"),
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
    rows.append(thin(w))
    # wrapped rather than clipped: the rule is the one line a narrow frame cannot lose
    rows.extend(f" {line}" for line in textwrap.wrap(QUIT_RULE, w - 1))
    title, table_rows = rows[:_TITLE_ROWS], rows[_TITLE_ROWS:]
    room = view.h - 1 - _TITLE_ROWS
    if len(table_rows) <= room:
        s.help_max = 0
        return build(view, rows, keybar([("Esc", "close")], w))
    # the last row of the room states the window, so one row fewer of the table shows
    shown = room - 1
    s.help_max, s.help_page = len(table_rows) - shown, shown
    s.help_top = max(0, min(s.help_top, s.help_max))
    win = RowWindow(start=s.help_top, stop=s.help_top + shown, total=len(table_rows))
    return build(
        view,
        [*title, *table_rows[win.start : win.stop], win.line()],
        keybar([("PageUp PageDown", "scroll"), ("Esc", "close")], w),
    )


def scroll_key(ctx: Ctx, key: str) -> bool:
    """Page the help card, claiming the paging keys whether or not it scrolls.

    Help has no cursor, so it pages rather than taking the arrows; on a card that fits
    the paging keys are a no-op, as the keybar does not offer them.

    Returns:
        Whether the key was a paging key, which the card then claimed.
    """
    if key not in _SCROLL_KEYS:
        return False
    s = ctx.s
    if not s.help_max:
        ctx.noop(key)
        return True
    step = s.help_page if key == "PageDown" else -s.help_page
    s.help_top = max(0, min(s.help_top + step, s.help_max))
    ctx.log(key, f"help rows from {s.help_top + 1}")
    return True
