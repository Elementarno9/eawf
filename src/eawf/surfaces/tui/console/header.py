"""The header: one row, the breadcrumb on the left and the state slot on the right.

The right side is the same at every frame size: an attention count only when something
needs the operator, then exactly one state value with its glyph. A session renders one of
the nine connection values; the pre-session layer renders its process value and no count.
A frame about an entity whose lifecycle has ended keeps its connection value: terminal is
a property of the entity, which the frame's own summary states, never a connection value.
The breadcrumb starts at the brand, and while the back stack holds history its middle is
that history, the path Escape walks, naming each place once. A crumb too wide for its room
gives way from the middle, never at the brand, the scope or the leaf. A drawn header is
read back into typed runs, so the painter styles each step without touching the text and a
step above the leaf is a link the pointer can walk to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

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
    session: Session, *, scope: str, leaf: str, registry: RouteRegistry, prototype: bool
) -> list[str]:
    """Return the crumb steps the back stack names, oldest first, each place once.

    A step drawn from what a link read never names the prototype record a route's declared
    leaf stands for; the prototype replay keeps it, because its goldens name it.
    """
    name = registry.step_leaf if prototype else registry.read_leaf
    entries = session.back.items()
    if entries and entries[0].route == HOME_ROUTE and not entries[0].subj:
        entries = entries[1:]
    steps = [name(entry.route, entry.subj) for entry in entries if entry.route != session.route]
    named = {BRAND, scope, leaf}
    chain: list[str] = []
    for step in reversed(steps):
        if step and step not in named:
            named.add(step)
            chain.append(step)
    chain.reverse()
    return chain


def crumb_from_history(
    session: Session,
    crumb: str,
    *,
    scope: str,
    registry: RouteRegistry = REGISTRY,
    prototype: bool = False,
) -> str:
    """Return ``crumb`` with its middle replaced by the back stack, when there is one.

    Args:
        session: The session whose back stack and route are read.
        crumb: The renderer's containment crumb; its last segment is the leaf.
        scope: The attached scope name, the step after the brand.
        registry: The route rows the step leaves come from.
        prototype: Whether the frame replays the prototype registers, whose steps may
            name the prototype records the routes' declared leaves stand for.

    Returns:
        ``crumb`` itself on an empty back stack, otherwise brand, scope, the history
        steps and the leaf, keeping ``crumb``'s leading gutter.
    """
    if not session.back:
        return crumb
    leaf = crumb.split(CRUMB_SEP)[-1]
    chain = _history_chain(session, scope=scope, leaf=leaf, registry=registry, prototype=prototype)
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


class CrumbPart(StrEnum):
    """What one run of a header row is, which fixes how it is styled and whether it links."""

    BRAND = "brand"
    SEP = "sep"
    STEP = "step"
    ID = "id"
    LEAF = "leaf"
    FOLD = "fold"
    SLOT = "slot"


@dataclass(frozen=True, slots=True, kw_only=True)
class CrumbRun:
    """One run of a header row's text and what it is.

    Attributes:
        text: The run's text, exactly as the row carries it.
        part: What the run is.
        start: The cell the run starts at.
        back: How many steps up from the leaf the run's step is; ``0`` for anything that
            is not a step above the leaf.
    """

    text: str
    part: CrumbPart
    start: int
    back: int = 0

    @property
    def end(self) -> int:
        """Return the cell one past the run's last."""
        return self.start + cell_len(self.text)

    @property
    def link(self) -> bool:
        """Return whether activating the run walks the breadcrumb to its step.

        The brand is not a place, a fold names no one place, and the leaf is where the
        operator already is, so going there is not a step.
        """
        return self.part in (CrumbPart.STEP, CrumbPart.ID) and self.back > 0


# A typed entity id: a capital prefix, a dash and the key the id grammar allows.
_TYPED_ID = re.compile(r"[A-Z][A-Z0-9]*-[0-9A-Za-z]+")
# The crumb ends where the padding before the state slot begins.
_CRUMB_END = re.compile(r"\s{2,}\S")


def crumb_runs(row: str) -> tuple[CrumbRun, ...]:
    """Return a header row cut into its runs, left to right, re-joining to ``row`` exactly.

    Args:
        row: A header row as :func:`header_row` returns it.

    Returns:
        The runs; the gutter and the padding before the state slot are ``SLOT`` runs.
    """
    gutter = _GUTTER.match(row)
    lead = gutter.group(0) if gutter else ""
    rest = row[len(lead) :]
    found = _CRUMB_END.search(rest)
    crumb, tail = (rest[: found.start()], rest[found.start() :]) if found else (rest, "")
    runs: list[CrumbRun] = []
    at = cell_len(lead)
    if lead:
        runs.append(CrumbRun(text=lead, part=CrumbPart.SLOT, start=0))
    steps = crumb.split(CRUMB_SEP) if crumb else []
    for i, step in enumerate(steps):
        if i:
            runs.append(CrumbRun(text=CRUMB_SEP, part=CrumbPart.SEP, start=at))
            at += cell_len(CRUMB_SEP)
        back = len(steps) - 1 - i
        if i == 0 and step == BRAND:
            part = CrumbPart.BRAND
        elif step == ELLIPSIS:
            part = CrumbPart.FOLD
        elif back == 0:
            part = CrumbPart.LEAF
        elif _TYPED_ID.fullmatch(step):
            part = CrumbPart.ID
        else:
            part = CrumbPart.STEP
        runs.append(CrumbRun(text=step, part=part, start=at, back=back))
        at += cell_len(step)
    if tail:
        runs.append(CrumbRun(text=tail, part=CrumbPart.SLOT, start=at))
    return tuple(runs)


def crumb_at(row: str, x: int) -> CrumbRun | None:
    """Return the linked crumb step drawn at cell ``x`` of ``row``, if any."""
    for run in crumb_runs(row):
        if run.start <= x < run.end:
            return run if run.link else None
    return None


def header_row(
    session: Session,
    *,
    crumb: str,
    scope: str,
    needs: int,
    w: int,
    process: ProcessValue | None = None,
    registry: RouteRegistry = REGISTRY,
    prototype: bool = False,
    actor: str | None = None,
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
        prototype: Whether the frame replays the prototype registers; see
            :func:`crumb_from_history`.
        actor: The principal key the console's writes go out as, named beside the count
            so the operator sees whose inbox and whose name every write carries; ``None``
            for a console that acts as nobody.

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
    acting = f"as {actor}  " if actor else ""
    slot = attention_count(needs) + acting + state_slot(session.conn)
    room = w - cell_len(slot)
    full = crumb.replace(SCOPE_SLOT, f"{CRUMB_SEP}{scope}{CRUMB_SEP}")
    if cell_len(full) <= room:
        crumb = full
    crumb = crumb_from_history(session, crumb, scope=scope, registry=registry, prototype=prototype)
    if cell_len(crumb) > room:
        crumb = _fold(crumb, room)
    return pad(crumb, room) + slot
