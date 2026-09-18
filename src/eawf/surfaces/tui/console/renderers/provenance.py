"""The native settings frames: what is in force, which layer set it, and what it overrode.

A settings value drawn without its layer is a value an operator cannot act on: the layer
that wins is the only place editing it has any effect, and a repo value that a local file
quietly overrides looks exactly like one that is in force. Both frames here answer that
second question from the read model rather than from a catalog of the console's own.

The list draws one row per leaf with the winning layer beside the value and the layers it
overrode after it. The stack card draws one leaf's whole ladder, lowest layer first, and
marks which entry is in force -- the same read answers both, so the card cannot disagree
with the row it was opened from.

Neither frame writes anything. The view is the daemon's answer to one read, and a change
to a value goes through the config verbs, never through a render.
"""

from __future__ import annotations

from eawf.kernel.projection.settings import SettingsLeaf, SettingsView
from eawf.kernel.projection.truth import TruthState
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Fixed, Table, View, bar, build, route_keys_bar, thin
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, truth_cell
from eawf.surfaces.tui.console.width import pad

#: What the overrode column shows for a leaf exactly one layer states. It is not a blank:
#: a blank would read as "this was not looked at" rather than "nothing was overridden".
NOTHING_OVERRIDDEN = "nothing"

#: How the stack card marks the entry whose value is in force, and the ones it overrode.
IN_FORCE = "in force"
OVERRIDDEN = "overridden"

_LEAVES = Table([38, 22, 11, 0], 2)
_STACK = Table([12, 30, 0], 2)
_EMPTY = "   this workspace composes no configuration leaf"
# The rows the list spends on chrome: the header, the counts, the rule, the column head,
# the closing rule, the window line, the read-only line and the keybar.
_LIST_CHROME = 8


def _crumb(scope: str) -> str:
    """Return the settings breadcrumb, ending at the route's own leaf."""
    return " " + CRUMB_SEP.join([BRAND, scope, "Settings"])


def _counts(settings: SettingsView) -> str:
    """Return what the read holds: its leaves, its sections and the cursor beside them."""
    layers = {leaf.winning_layer for leaf in settings.leaves}
    return (
        f" {group(len(settings.leaves))} leaves · {group(len(settings.sections()))} sections · "
        f"{group(len(layers))} winning layers · cursor {group(int(settings.header.source_cursor))}"
    )


def focused(session: Session, settings: SettingsView) -> int | None:
    """Return the row the cursor sits on, clamped into the read, and publish it.

    Args:
        session: The session whose ``sel`` is the row the cursor was on.
        settings: The read model the frame draws.

    Returns:
        The leaf offset the caret goes on, or ``None`` when the read states no leaf at
        all, which draws no caret.
    """
    if not settings.leaves:
        session.sel = 0
        return None
    index = min(max(session.sel, 0), len(settings.leaves) - 1)
    session.sel = index
    return index


def _effective(leaf: SettingsLeaf) -> str:
    """Return a leaf's effective value, or the unknown token when no layer states it."""
    field = leaf.effective
    if field.state is not TruthState.KNOWN or field.value is None:
        return truth_cell("unknown")
    return field.value


def window(total: int, *, cursor: int, take: int) -> int:
    """Return the row a window of ``take`` rows starts at, with the cursor inside it.

    A settings read states every leaf the merge placed, which is far more than a frame
    holds, so the list is a window rather than the head of the list: a cursor walked past
    the last drawn row would otherwise select a leaf nothing showed.

    Args:
        total: How many rows the read states.
        cursor: The row the caret sits on.
        take: How many rows the frame has room for; at least one.

    Returns:
        The first row index to draw.

    Raises:
        ValueError: ``take`` is not positive, so no window exists.
    """
    if take < 1:
        raise ValueError(f"a settings window needs at least one row, got {take}")
    if total <= take:
        return 0
    return max(0, min(cursor - take // 2, total - take))


def list_frame(view: View, settings: SettingsView) -> list[str]:
    """Return the settings list: every leaf with its layer and what that layer overrode.

    Args:
        view: The render being built; its session carries the cursor.
        settings: The effective-settings read model.

    Returns:
        The full frame, keybar last, windowed onto the leaf the cursor sits on.
    """
    session, w = view.session, view.w
    scope = settings.header.scope_id
    cursor = focused(session, settings)
    total = len(settings.leaves)
    take = max(1, view.h - _LIST_CHROME)
    start = window(total, cursor=cursor or 0, take=take)
    rows: list[str] = [
        header_row(session, crumb=_crumb(scope), scope=scope, needs=0, w=w),
        _counts(settings),
        bar(w),
        _LEAVES.head(["LEAF", "EFFECTIVE", "LAYER", "OVERRODE"]),
    ]
    if not settings.leaves:
        rows.append(_EMPTY)
    for index, row in enumerate(settings.leaves[start : start + take], start=start):
        overrode = ", ".join(row.overridden()) or NOTHING_OVERRIDDEN
        line = _LEAVES.row([row.key, _effective(row), row.winning_layer, overrode], index == cursor)
        rows.append(line if index == cursor else Fixed(pad(line, w)))
    rows.append(thin(w))
    shown = min(total, start + take)
    rows.append(f" WINDOW     {start + bool(total)}-{shown} of {group(total)} leaves")
    rows.append(" READ ONLY  a value is changed through the config verbs, never from here.")
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS[session.route]))


def stack_frame(view: View, settings: SettingsView) -> list[str]:
    """Return the stack card of the focused leaf: every layer that states it, lowest first.

    Args:
        view: The render being built; its session carries the cursor the card opened on.
        settings: The effective-settings read model, the same one the list drew from.

    Returns:
        The full frame, keybar last; the list frame when the read states no leaf to open.
    """
    session, w = view.session, view.w
    scope = settings.header.scope_id
    cursor = focused(session, settings)
    if cursor is None:
        return list_frame(view, settings)
    leaf = settings.leaves[cursor]
    rows: list[str] = [
        header_row(session, crumb=_crumb(scope), scope=scope, needs=0, w=w),
        f" {leaf.key} · in force from {leaf.winning_layer}",
        bar(w),
        _STACK.head(["LAYER", "VALUE", "STANDING"]),
    ]
    for entry in leaf.stack:
        line = _STACK.row(
            [entry.layer, entry.value, IN_FORCE if entry.wins else OVERRIDDEN], entry.wins
        )
        rows.append(line if entry.wins else Fixed(pad(line, w)))
    rows.append(thin(w))
    overrode = ", ".join(leaf.overridden()) or NOTHING_OVERRIDDEN
    rows.append(f" OVERRODE   {overrode}")
    rows.append(f" EFFECTIVE  {_effective(leaf)}")
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS[session.route]))
