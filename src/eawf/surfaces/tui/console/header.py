"""The header: one row, the breadcrumb on the left and the state slot on the right.

The right side is the same at every frame size: an attention count only when something
needs the operator, then exactly one state value with its glyph. A session renders one of
the nine connection values; the pre-session layer renders its process value and no count.
The breadcrumb starts at the brand, and while the back stack holds history its middle is
that history, the path Escape walks, naming each place once. A crumb too wide for its room
gives way from the middle, never at the brand, the scope or the leaf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.registry import REGISTRY, RouteRegistry
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CONNECTION, CRUMB_SEP
from eawf.surfaces.tui.console.width import cell_len, pad

ELLIPSIS = "…"
# A renderer writes this segment where the scope goes; the header fills it in when it fits.
SCOPE_SLOT = f"{CRUMB_SEP}{ELLIPSIS}{CRUMB_SEP}"
ENTRY_ROUTE = "entry"
HOME_ROUTE = "scope.home"
_GUTTER = re.compile(r"\s*")
# The crumb keeps the brand, the scope and the leaf; only the history between them folds.
_KEPT_HEAD = 2


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessValue:
    """The state a pre-session header shows in place of a connection value.

    Attributes:
        glyph: The one-cell marker of the process state.
        label: The upper-case process word, such as ``RESOLVING``.

    Raises:
        ValueError: ``glyph`` is not one cell, or ``label`` is blank.
    """

    glyph: str
    label: str

    def __post_init__(self) -> None:
        """Reject a marker that would shift the slot, or an empty state."""
        if cell_len(self.glyph) != 1:
            raise ValueError(f"process glyph {self.glyph!r} must occupy exactly one cell")
        if not self.label.strip():
            raise ValueError("a process value needs a label")


def state_slot(conn: str) -> str:
    """Return the connection value as the header prints it: glyph, space, label.

    Raises:
        ValueError: ``conn`` is not one of the nine connection values.
    """
    glyph = CONNECTION.get(conn)
    if glyph is None:
        raise ValueError(f"{conn!r} is not a connection value")
    return f"{glyph.unicode} {conn}"


def attention_count(needs: int) -> str:
    """Return the header attention count, or nothing when nothing needs the operator.

    Raises:
        ValueError: ``needs`` is negative.
    """
    if needs < 0:
        raise ValueError(f"an attention count cannot be negative, got {needs}")
    return f"!{group(needs)} NEEDS YOU  " if needs else ""


def _history_chain(
    session: Session, *, scope: str, leaf: str, registry: RouteRegistry
) -> list[str]:
    """Return the crumb steps the back stack names, oldest first, each place once."""
    entries = session.back.items()
    if entries and entries[0].route == HOME_ROUTE and not entries[0].subj:
        entries = entries[1:]
    steps = [
        registry.step_leaf(entry.route, entry.subj)
        for entry in entries
        if entry.route != session.route
    ]
    named = {BRAND, scope, leaf}
    chain: list[str] = []
    for step in reversed(steps):
        if step and step not in named:
            named.add(step)
            chain.append(step)
    chain.reverse()
    return chain


def crumb_from_history(
    session: Session, crumb: str, *, scope: str, registry: RouteRegistry = REGISTRY
) -> str:
    """Return ``crumb`` with its middle replaced by the back stack, when there is one.

    Args:
        session: The session whose back stack and route are read.
        crumb: The renderer's containment crumb; its last segment is the leaf.
        scope: The attached scope name, the step after the brand.
        registry: The route rows the step leaves come from.

    Returns:
        ``crumb`` itself on an empty back stack, otherwise brand, scope, the history
        steps and the leaf, keeping ``crumb``'s leading gutter.
    """
    if not session.back:
        return crumb
    leaf = crumb.split(CRUMB_SEP)[-1]
    chain = _history_chain(session, scope=scope, leaf=leaf, registry=registry)
    gutter = _GUTTER.match(crumb)
    lead = gutter.group(0) if gutter else ""
    return lead + CRUMB_SEP.join([BRAND, scope, *chain, leaf])


def _fold(crumb: str, room: int) -> str:
    """Fold the history steps into one ellipsis until the crumb fits ``room`` cells."""
    steps = crumb.split(CRUMB_SEP)
    while cell_len(CRUMB_SEP.join(steps)) > room:
        middle = steps[_KEPT_HEAD:-1]
        if not middle or middle == [ELLIPSIS]:
            break
        if middle[0] == ELLIPSIS:
            del steps[_KEPT_HEAD + 1]
        else:
            steps[_KEPT_HEAD] = ELLIPSIS
    return CRUMB_SEP.join(steps)


def header_row(
    session: Session,
    *,
    crumb: str,
    scope: str,
    needs: int,
    w: int,
    process: ProcessValue | None = None,
    registry: RouteRegistry = REGISTRY,
) -> str:
    """Return the header row, exactly ``w`` cells.

    Args:
        session: The session whose route, connection value and back stack are read.
        crumb: The renderer's crumb, starting at the brand; a ``SCOPE_SLOT`` in it is
            filled with ``scope`` when the result fits.
        scope: The attached scope name.
        needs: This principal's open attention count; shown only when positive.
        w: The frame width in cells.
        process: The pre-session process value; given exactly when the session is on the
            ``entry`` layer, where no count and no connection value render.
        registry: The route rows the history steps are named from.

    Raises:
        ValueError: ``process`` is given off the entry layer or missing on it, ``needs``
            is negative, ``session.conn`` is not a connection value, or the state slot
            does not fit in ``w`` cells.
    """
    if (session.route == ENTRY_ROUTE) != (process is not None):
        raise ValueError("the entry layer's header shows a process value and no other route's does")
    if process is not None:
        slot = f"{process.glyph} {process.label}"
        return pad(crumb, w - cell_len(slot)) + slot
    slot = attention_count(needs) + state_slot(session.conn)
    room = w - cell_len(slot)
    full = crumb.replace(SCOPE_SLOT, f"{CRUMB_SEP}{scope}{CRUMB_SEP}")
    if cell_len(full) <= room:
        crumb = full
    crumb = crumb_from_history(session, crumb, scope=scope, registry=registry)
    if cell_len(crumb) > room:
        crumb = _fold(crumb, room)
    return pad(crumb, room) + slot
