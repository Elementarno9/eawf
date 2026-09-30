"""Frame composition: the header and keybar rows, rules, column tables and ``build``.

A renderer returns ``build(view, rows, keys)``: exactly H rows of W cells with the keybar
last. ``build`` pads and clips the body rows, snaps a gap-separated cursor against the row
it marks, paints the ``--verbose`` trace row and the notification rack over the body, and
swaps the keybar of an absent or record frame. A row a renderer has already laid out is
marked :class:`Fixed` so ``build`` neither snaps nor clips it.

Some rows carry chip markers: control characters that colour a cell without taking a
column. The plain-text path strips them, and every helper here measures a row without
them, so a renderer can keep the markers in place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from types import MappingProxyType

from eawf.kernel.projection.attention import attention_mine
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.connection import ReplayNote
from eawf.kernel.projection.liveness import HeldLiveness
from eawf.kernel.projection.registers import RegisterView
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.run_timeline import RunTimeline
from eawf.kernel.projection.settings import EffectiveSettingsView
from eawf.kernel.projection.spine import SpineView
from eawf.kernel.projection.truth import TruthState
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.surfaces.tui.console.attention import open_count
from eawf.surfaces.tui.console.chrome import EntryState
from eawf.surfaces.tui.console.decisions import DecisionRecords
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.header import ProcessValue, header_row
from eawf.surfaces.tui.console.keybar import (
    KEY,
    KEY_NAMES,
    RECORD_FRAME_KEYS,
    ROUTE_KEYS,
    KeyEntry,
    Pair,
    keybar,
)
from eawf.surfaces.tui.console.keymap import unserved
from eawf.surfaces.tui.console.session import Session, Toast
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, RULE_HEAVY, RULE_THIN, Severity
from eawf.surfaces.tui.console.width import cell_len, clip_words, pad

CARET = "▸"
#: The smallest windowable region: an above count, a row and a below count.
MIN_WINDOW = 3
_CARET_GAP = re.compile(r"^(\s*)▸(\s{2,})(\S)")
_GUTTER = 13
#: The terminal widths the layouts step at: from :data:`WIDE_FROM` columns a frame takes
#: its wide layout and from :data:`XWIDE_FROM` its widest, as the pack's 80/120/160 do.
WIDE_FROM = 120
XWIDE_FROM = 160


class Breadth(IntEnum):
    """The layout a terminal's width selects, ordered so a wider one compares greater."""

    NARROW = 0
    WIDE = 1
    XWIDE = 2


def breadth_of(columns: int) -> Breadth:
    """Return the layout a terminal ``columns`` wide takes.

    Args:
        columns: The terminal's width, gutters included -- never the frame's.

    Returns:
        The layout every width breakpoint of the frame steps on.
    """
    if columns >= XWIDE_FROM:
        return Breadth.XWIDE
    return Breadth.WIDE if columns >= WIDE_FROM else Breadth.NARROW


@dataclass(frozen=True, slots=True, kw_only=True)
class View:
    """What one render reads.

    Attributes:
        session: The session the render reads and publishes into.
        fixture: The registers; one built from the packaged chrome holds no prototype row.
        w: The frame width in cells.
        h: The frame height in rows.
        verbose: Whether the ``--verbose`` trace row is painted.
        held: Whether the console clock is held, which freezes every live feed.
        projection: The daemon-served read model of the session's route, when the console
            is bound to one. The spine routes hold a :class:`SpineView`; every other
            bound family holds a :class:`RouteReadModel`. ``None`` is the epoch-1 mode
            the prototype registers drive, which the tracked golden contract replays.
        register: The daemon-served register read model of the session's route, under
            the same rule as ``projection``: a route is either a spine route or a
            register route, so at most one of the two is ever held.
        attention: The Attention register, held whatever route is drawn, because the
            header prints its count on every frame. It is the same object as
            ``register`` while the session sits on the Attention route.
        settings: The daemon-served effective-settings view, held on the settings
            routes under the same rule: ``None`` draws the prototype catalog.
        linked: Whether the console holds a daemon link. A linked console states only
            what the link has read, so its prototype registers are never counted.
        principal_refusal: Why every bound write is refused because the link acts as
            nobody; empty when it acts as someone or there is no link.
        replay: The replay the link is carrying out, while it is replaying: where it
            started and the head it heads toward. ``None`` at any other time.
        rows: Every row the link's held projections carry, which the action menu judges
            each lifecycle verb against.
        decisions: The records the decision overlays and cards are bound to, which
            arrive beside the projection; ``None`` when none are held.
        notices: The budget notices active in the operator's inbox, which the Attention
            route lists after its actions; empty before their read or with no link.
        liveness: The daemon's stall read, which says which running Runs went quiet and
            how old that answer is; ``None`` before it arrives or with no link.
        timeline: The Run frame's event rows as the daemon grouped them, for the Run the
            frame is about; ``None`` before their read or off the Run frame.
        principal: Who the console acts as, whose own attention items the header counts;
            ``None`` when it acts as nobody, which has no ``mine`` to count.
        now: The wall-clock instant the frame is drawn at, which an age or a running
            elapsed time is measured to; ``None`` states those as of the read instead.
        scope_name: The name the header gives the attached scope, such as the project's
            own name; empty names it by the id the projection was read for.
        gutter: The blank cells the app keeps clear at each side of the frame. The frame
            is ``w`` cells inside them, but a layout steps on the terminal's width, so a
            120-column terminal takes the wide layout although its frame is 118 wide.
        live: The answers of the route's live reads, by read name, for what the route
            is about now; empty before they arrive or with no link.
    """

    session: Session
    fixture: Fixture
    w: int
    h: int
    verbose: bool = False
    held: bool = False
    projection: SpineView | RouteReadModel | None = None
    register: RegisterView | None = None
    attention: RegisterView | None = None
    settings: EffectiveSettingsView | None = None
    linked: bool = False
    principal_refusal: str = ""
    replay: ReplayNote | None = None
    rows: tuple[ProjectionRow, ...] = ()
    decisions: DecisionRecords | None = None
    notices: tuple[BudgetThresholdNotice, ...] = ()
    liveness: HeldLiveness | None = None
    timeline: RunTimeline | None = None
    principal: str | None = None
    now: datetime | None = None
    scope_name: str = ""
    gutter: int = 0
    live: Mapping[str, object] = MappingProxyType({})

    @property
    def columns(self) -> int:
        """Return the terminal's width: the frame plus both gutters."""
        return self.w + 2 * self.gutter

    @property
    def breadth(self) -> Breadth:
        """Return the layout the terminal's width selects."""
        return breadth_of(self.columns)

    @property
    def wide(self) -> bool:
        """Return whether the terminal takes the wide layout or a wider one."""
        return self.breadth >= Breadth.WIDE

    @property
    def xwide(self) -> bool:
        """Return whether the terminal takes the widest layout."""
        return self.breadth is Breadth.XWIDE


def scope_label(view: View, scope_id: str) -> str:
    """Return the step the header names the scope by: its name, else ``scope_id``.

    The id stays what the projection is addressed by; the crumb is read by an operator,
    who knows the project by its name.
    """
    return view.scope_name or scope_id


def unheld(view: View) -> bool:
    """Return whether the frame has nothing to draw: no prototype rows, no read model held.

    A console built from the packaged chrome alone draws a route only from what its seam
    holds; anything else would be an empty prototype frame posing as a real one.
    """
    return (
        not view.fixture.prototype
        and view.projection is None
        and view.register is None
        and view.settings is None
    )


def from_read_model(view: View) -> bool:
    """Return whether the frame is drawn from what a link read, not the prototype registers.

    The golden contract replays the prototype registers verbatim, keybar included, so a
    rule that trims a keybar to what the frame holds applies to read-model frames only.
    """
    return not view.fixture.prototype or view.projection is not None or view.register is not None


class Fixed(str):
    """A row ``build`` neither snaps nor clips: it is already laid out."""

    __slots__ = ()


class Receded(Fixed):
    """A laid-out row of a pane that does not own the arrows, which the painter recedes.

    Which pane holds the focus is a fact about the render, not about the words, so the
    renderer that knows it marks the rows rather than the painter guessing from the text.
    """

    __slots__ = ()


def recede(line: str, w: int) -> Receded:
    """Return ``line`` laid out to ``w`` cells and marked as a receded pane's row."""
    return Receded(pad(line, w))


class RailReceded(Fixed):
    """A laid-out row whose rail entry, the part after its last rail glyph, recedes.

    A bucket rail shares its rows with the list beside it, so a bucket the filter leaves
    out recedes on its own while the list row it sits beside is drawn as usual.
    """

    __slots__ = ()


class Lensed(Fixed):
    """A laid-out row holding the settings lens strip, which the painter marks by layer.

    Which layer the lens writes to and which layers set the focused key are facts about
    the render, not about the words, so the strip names the layers plainly and the row
    carries the rest.

    Attributes:
        lens: The layer the lens writes to, drawn bold.
        layers: The surface each layer that sets the focused key is drawn in.
    """

    # a str subclass can hold no non-empty slots, so these two live in the instance dict
    lens: str
    layers: Mapping[str, str]

    def __new__(cls, text: str, *, lens: str, layers: Mapping[str, str]) -> Lensed:
        row = super().__new__(cls, text)
        row.lens = lens
        row.layers = layers
        return row


def recede_rail(line: str, w: int) -> RailReceded:
    """Return ``line`` laid out to ``w`` cells with its rail entry marked as receded."""
    return RailReceded(pad(line, w))


class LeadReceded(Fixed):
    """A laid-out row whose rail, the part before its first rail glyph, recedes.

    While an editor is open the rail beside it does not own the arrows, so the rail
    recedes on every row, the editor's own rows beside it included.
    """

    __slots__ = ()


class Titled(Fixed):
    """A laid-out row opening a pane: its label is bold and the rest is drawn plain.

    What follows the label is a legend to the pane's glyphs, not a row of column heads,
    though it is written in capitals as heads are.
    """

    __slots__ = ()


def bar(w: int) -> str:
    """Return the heavy rule under a frame's title rows."""
    return RULE_HEAVY * w


def thin(w: int) -> str:
    """Return the thin rule between a frame's sections."""
    return RULE_THIN * w


def snap_caret(row: str) -> str:
    """Move a gap-separated cursor against the row it marks, keeping the row's width."""
    found = _CARET_GAP.match(row)
    if not found:
        return row
    gap = found.group(2)
    return found.group(1) + " " * (len(gap) - 1) + f"{CARET} " + row[len(found.group(0)) - 1 :]


def needs_count(view: View) -> int:
    """Return the header's attention count: the held Attention register's, and only that.

    The count has one producer whatever route is drawn, so the header's ``!N`` and the
    Attention frame's ``mine`` row cannot disagree. It is this principal's own count, so a
    console acting as nobody states none and the header shows no badge for it -- the
    badge is absent, which is what it already is at zero, rather than another principal's
    number standing in for one this console has not got. A linked
    console that has read no register yet shows none either, whatever prototype rows it
    carries; only a console with no link at all holds its prototype register as the
    register, which is the mode the tracked golden contract replays.
    """
    held = view.attention
    if held is None:
        return 0 if view.linked else open_count(view.fixture)
    mine = attention_mine(held, principal=view.principal)
    return int(mine.value) if mine.state is TruthState.KNOWN and mine.value else 0


def header(view: View, crumb: str) -> str:
    """Return the frame's header row for ``crumb``.

    The pre-session layer shows its process value; every other route shows the open
    attention count, derived from the register now, and the connection value.
    """
    session, proto = view.session, view.fixture.proto
    if session.route == "entry":
        state = entry_state(view)
        process = ProcessValue(glyph=state.glyph, label=state.state)
        return header_row(
            session, crumb=crumb, scope=proto.scope, needs=0, w=view.w, process=process
        )
    scope = proto.scope
    if not view.fixture.prototype and view.scope_name:
        # a linked console knows the project it is open in and names it on every frame
        scope = view.scope_name
        crumb = _scoped(crumb, scope, proto.scope)
    return header_row(
        session,
        crumb=crumb,
        scope=scope,
        needs=needs_count(view),
        w=view.w,
        prototype=view.fixture.prototype,
    )


def _scoped(crumb: str, scope: str, placeholder: str) -> str:
    """Return ``crumb`` naming the attached project as the step after the brand.

    An overlay's crumb is written from the brand to the overlay; on a linked console the
    project it is open in is named before it, as on every route frame, and a crumb that
    names the chrome's placeholder scope names the project instead.
    """
    lead, _, rest = crumb.partition(BRAND)
    steps = rest.split(CRUMB_SEP)[1:] if rest.startswith(CRUMB_SEP) else []
    if not steps or steps[0] == scope:
        return crumb
    if steps[0] == placeholder:
        steps = steps[1:]
    return lead + CRUMB_SEP.join([BRAND, scope, *steps])


def entry_state(view: View) -> EntryState:
    """Return the entry layer's current pre-session state, the first past the end."""
    states = view.fixture.proto.entry
    sel = view.session.entry_sel
    return states[sel] if sel < len(states) else states[0]


class Table:
    """A table's columns declared once, composing both its head and its rows.

    Args:
        cols: Column widths; the last column is the remainder and is never padded.
        mark: The cursor gutter's width, which the head clears too.
    """

    __slots__ = ("cols", "mark")

    def __init__(self, cols: Sequence[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: Sequence[str]) -> str:
        """Return the head row."""
        out = " " + " " * self.mark
        for i, name in enumerate(names):
            out += pad(name, self.cols[i]) if i < len(self.cols) - 1 else name
        return out

    def row(self, cells: Sequence[str], cur: bool = False) -> str:
        """Return one row, the cursor in the gutter when ``cur``."""
        lead = (CARET + " " * (self.mark - 1) if cur else " " * self.mark) if self.mark else ""
        out = " " + lead
        for i, cell in enumerate(cells):
            out += pad(cell, self.cols[i]) if i < len(self.cols) - 1 else cell
        return out


TABLES: Mapping[str, Table] = MappingProxyType(
    {
        "RD": Table([25, 11, 0], 4),
        "RC": Table([11, 38, 9, 0], 4),
        "CR": Table([40, 0], 2),
        "MB": Table([34, 11, 0]),
        "RN": Table([34, 10, 12, 0]),
    }
)


def route_keys_bar(view: View, entries: Sequence[KeyEntry]) -> str:
    """Return a route keybar; a record frame with fewer than two rows drops ``↑↓ row``."""
    nav = view.session.record_nav
    one_row = nav is not None and len(nav) < 2
    pairs = [e.pair() for e in entries if not (one_row and e == KEY["up"])]
    return keybar(acting_pairs(view, pairs), view.w, keep_actions=from_read_model(view))


def acting_pairs(view: View, pairs: Sequence[Pair]) -> list[Pair]:
    """Return the pairs whose keys act on what the frame drew; the rest are left off.

    Keys shown are the keys that work, and this is the one rule every read-model keybar
    passes through. Paging and the ends act only when a table on the frame was cut to its
    window (``session.windowed``). A frame that walks rows publishes how many its cursor
    walks as ``session.nav_rows``: with fewer than two the arrows have nothing to move
    between, and with none Enter has no row to open. A key the route's native frame does
    not serve at all is left off whatever it holds. The prototype replay keeps its whole
    table, because its keybars are the golden ones.

    The keys kept are published as ``session.bar_keys``: a motion key outside them has no
    cursor on the frame to move, so it is refused rather than moving an undrawn one.
    """
    if not from_read_model(view):
        return list(pairs)
    session = view.session
    idle = unserved(session.route, session.subj_id)
    acting = [
        (token, label)
        for token, label in pairs
        if not idle.intersection(_keys_of(token)) and _acts(token, session)
    ]
    session.bar_keys = frozenset(key for token, _label in acting for key in _keys_of(token))
    return acting


_KEY_OF_NAME: Mapping[str, str] = MappingProxyType({name: key for key, name in KEY_NAMES.items()})


def _keys_of(token: str) -> list[str]:
    """Return the dispatcher key names a keybar token prints, a glyph pair split in two."""
    keys: list[str] = []
    for word in token.split():
        glyphs = list(word) if all(ch in _KEY_OF_NAME for ch in word) else [word]
        keys.extend(_KEY_OF_NAME.get(glyph, glyph) for glyph in glyphs)
    return keys


def _acts(token: str, session: Session) -> bool:
    """Return whether the keys ``token`` names act on the frame ``session`` published."""
    if token in (KEY["page"].token, KEY["ends"].token):
        return session.windowed
    held = session.nav_rows
    if held is None:
        return True
    if token == KEY["up"].token:
        return held > 1
    if token == KEY["enter"].token:
        return held > 0
    return True


@dataclass(frozen=True, slots=True, kw_only=True)
class RowWindow:
    """The slice of a table a frame draws.

    Attributes:
        start: The offset of the first row drawn.
        stop: One past the offset of the last row drawn.
        total: The rows the table holds, drawn or not.
    """

    start: int
    stop: int
    total: int

    @property
    def hides(self) -> bool:
        """Whether a row of the table falls outside the window, which a ``WINDOW`` row owes."""
        return self.start > 0 or self.stop < self.total

    def line(self, *, complete: bool = True) -> str:
        """Return the ``WINDOW`` row, which says how much of the table is off screen.

        Args:
            complete: Whether the table claims every row; a partial one says ``known``,
                because its total is a floor rather than a count.
        """
        return f" WINDOW    {self.count(complete=complete)}"

    def count(self, *, complete: bool = True) -> str:
        """Return what the ``WINDOW`` row states after its label: the span and the total.

        Args:
            complete: Whether the table claims every row; a partial one says ``known``.
        """
        span = f"{group(self.start + 1)}–{group(self.stop)}" if self.stop > self.start else "0"  # noqa: RUF001
        return f"{span} of {group(self.total)}" + ("" if complete else " known")


def window_rows(view: View, *, total: int, cursor: int, chrome: int) -> RowWindow:
    """Return the rows of a table that fit the frame, the cursor always among them.

    The window moves only when the cursor would leave it, so a keystroke inside the
    window never scrolls. ``session.scroll`` carries the window between renders and
    ``session.visible`` is published because the dispatcher pages by it.

    Args:
        view: The render being built; its height and the rack's reserved rows bound the
            window.
        total: The rows the table holds.
        cursor: The row offset the caret sits on.
        chrome: Every row of the frame that is not a table row, the keybar excepted.

    Returns:
        The window, never under :data:`MIN_WINDOW` rows however little room the frame
        leaves: a region shorter than that cannot carry its edge counts and a row.
    """
    session = view.session
    room = max(MIN_WINDOW, view.h - 1 - chrome - session.reserved)
    start = max(min(session.scroll, cursor), cursor - room + 1)
    start = max(0, min(start, total - room))
    session.scroll, session.visible = start, room
    session.windowed = session.windowed or total > room
    session.nav_rows = total
    return RowWindow(start=start, stop=min(total, start + room), total=total)


def _swapped_keys(session: Session, keys: str, w: int) -> str:
    """Return the keybar an absent or record frame shows in place of the route's."""
    if session.absent:
        return keybar([KEY["esc"].pair()], w)
    if session.record_facts is None:
        return keys
    nav = session.record_nav or []
    lead = [KEY["up"], KEY["enter"]] if len(nav) > 1 else ([KEY["enter"]] if nav else [])
    entries = [*lead, *RECORD_FRAME_KEYS]
    return keybar([entry.pair() for entry in entries], w)


def make_room(body: Sequence[str], n: int) -> list[str] | None:
    """Return ``body`` less ``n`` rows that state nothing, so ``n`` rows can go above the keybar.

    The legend row budget: trailing blank rows go first, then thin rules from the bottom
    up. A rule separates sections but states no fact, so a frame whose every row is a
    fact keeps its sections and gives up only their dividers.

    Args:
        body: The frame rows above the keybar.
        n: How many rows to free.

    Returns:
        The kept rows, ``n`` fewer than ``body``; ``None`` when fewer than ``n`` rows state
        nothing, because freeing more would drop a fact.

    Raises:
        ValueError: ``n`` is negative.
    """
    if n < 0:
        raise ValueError(f"cannot free a negative number of rows, got {n}")
    kept = list(body)
    need = n
    while need and kept and not kept[-1].strip():
        kept.pop()
        need -= 1
    for i in range(len(kept) - 1, -1, -1):
        if not need:
            break
        if kept[i] and set(kept[i]) == {RULE_THIN}:
            del kept[i]
            need -= 1
    return kept if not need else None


# The padding between a header's crumb and its state slot.
_SLOT_GAP = re.compile(r" {3,}(?=\S)")


def _inset(head: str) -> str:
    """Return the header with its state slot one cell in from the band's right edge.

    The crumb opens one cell inside the band, so the slot closes one cell inside it too; the
    cell comes out of the padding before the slot, which keeps the two cells it needs.
    """
    gap = _SLOT_GAP.search(head)
    if gap is None or head.endswith(" "):
        return head
    return head[: gap.start()] + head[gap.start() + 1 :] + " "


def _laid(row: str, w: int) -> str:
    """Return ``row`` at ``w`` cells: a fixed row padded as it is, a receded one kept so."""
    if isinstance(row, (Receded, RailReceded, LeadReceded)):
        return type(row)(row + " " * max(0, w - cell_len(row)))
    if isinstance(row, Fixed):
        # a row already as wide as the frame keeps its type, and what its type tells
        gap = w - cell_len(row)
        return row + " " * gap if gap > 0 else row
    return pad(snap_caret(row), w)


#: Every token a keybar pair can open with, longest first so ``PageUp PageDown`` is read
#: as one token rather than as ``PageUp`` and a label.
_BAR_TOKENS: tuple[str, ...] = tuple(
    sorted(
        {e.token for table in ROUTE_KEYS.values() for e in table} | {e.token for e in KEY.values()},
        key=len,
        reverse=True,
    )
)


def bar_keys(bar: str) -> frozenset[str]:
    """Return the dispatcher keys a drawn keybar row offers.

    A frame whose pairs passed :func:`acting_pairs` has published them already, before a
    narrow bar gave up its paging pairs for width; any other frame's offer is read back
    off the row it drew. A legend docked beside the pairs opens with no key token and is
    skipped.
    """
    keys: set[str] = set()
    for pair in re.split(r" {3,}", bar.strip()):
        token = next((t for t in _BAR_TOKENS if pair.startswith(f"{t} ")), None)
        if token is not None:
            keys.update(_keys_of(token))
    return frozenset(keys)


def build(view: View, rows: Sequence[str], keys: str) -> list[str]:
    """Return the full frame: H rows of W cells, the keybar last.

    Args:
        view: The render being built.
        rows: The header, the subtitle and the body rows, top to bottom.
        keys: The route's keybar row.

    Raises:
        ValueError: a composed row is not exactly W cells.
    """
    session, w, h = view.session, view.w, view.h
    session.absent_frame = session.absent
    keys = _swapped_keys(session, keys, w)
    if session.bar_keys is None:
        session.bar_keys = bar_keys(keys)
    session.absent = False
    out: list[str] = [_laid(row, w) for row in rows[: h - 1]]
    if view.gutter and out:
        out[0] = _inset(out[0])
    out.extend(" " * w for _ in range(h - 1 - len(out)))
    paint_marks(session, out)
    if view.verbose:
        paint_verbose(session, out, w)
    paint_rack(session, out, w, verbose=view.verbose)
    out.append(keys)
    for i, row in enumerate(out):
        if cell_len(row) != w:
            raise ValueError(f"row {i} is {cell_len(row)} cells, not {w}: {row!r}")
    return out


# ---------- chip markers and the table grid most routes share ----------

_CHIPS = re.compile("[\\x01-\\x07\\x10-\\x12]")
_CHIP_MARKS: Mapping[str, str] = MappingProxyType(
    {"ok": "\x01", "wn": "\x02", "er": "\x03", "info": "\x04", "dm": "\x05"}
)
CHIP_END = "\x06"
LABEL_MARK = "\x07"


def strip_chips(text: str) -> str:
    """Return ``text`` without its chip markers."""
    return _CHIPS.sub("", text)


def chip(kind: str, text: str) -> str:
    """Return ``text`` marked as a chip of ``kind``; the marker never takes a column.

    Raises:
        KeyError: ``kind`` names no chip.
    """
    return _CHIP_MARKS[kind] + text + CHIP_END


def g_pad(text: str, n: int) -> str:
    """Return ``text`` as ``n`` visible cells, measured without its chip markers."""
    vis = cell_len(strip_chips(text))
    if vis > n:
        return pad(strip_chips(text), n)
    return text if vis == n else text + " " * (n - vis)


def lab(name: str, text: str = "") -> str:
    """Return a labelled row: the label in the thirteen-cell gutter, then ``text``."""
    return g_pad(f" {LABEL_MARK}{name}{CHIP_END}", _GUTTER) + text


def rule_n(n: int) -> str:
    """Return a thin rule ``n`` cells wide."""
    return RULE_THIN * n


class Grid:
    """A table whose last cell takes the room left and whose cells ignore chip markers.

    An over-wide cell gives way to its width less one plus a space, so the next column
    never moves.

    Args:
        cols: Column widths; the last column is the remainder.
        mark: The cursor gutter's width.
    """

    __slots__ = ("cols", "mark")

    def __init__(self, cols: Sequence[int], mark: int = 2) -> None:
        self.cols = list(cols)
        self.mark = mark

    def head(self, names: Sequence[str]) -> str:
        """Return the head row."""
        out = " " + " " * self.mark
        for i, name in enumerate(names):
            out += g_pad(name, self.cols[i]) if i < len(self.cols) - 1 else name
        return out

    def row(self, cells: Sequence[str | None], cur: bool, w: int) -> str:
        """Return one row of a ``w``-cell frame, the cursor in the gutter when ``cur``."""
        lead = " "
        if self.mark:
            lead += CARET + " " * (self.mark - 1) if cur else " " * self.mark
        out = lead
        used = cell_len(strip_chips(lead))
        for i, raw in enumerate(cells):
            text = "" if raw is None else raw
            vis = cell_len(strip_chips(text))
            if i >= len(self.cols) - 1:
                room = w - used
                out += pad(strip_chips(text), max(1, room)) if vis > room else text
                break
            width = self.cols[i]
            if vis >= width:
                out += pad(strip_chips(text), max(1, width - 1)) + " "
            else:
                out += text + " " * (width - vis)
            used += width
        return out


def g_row(line: str, w: int) -> Fixed:
    """Return a laid-out row: cursor snapped, markers stripped, padded to the frame."""
    return Fixed(pad(strip_chips(snap_caret(line)), w))


def g_frame(
    view: View, *, crumb: str, ctx: str, body: Sequence[str], keys: Sequence[Pair]
) -> list[str]:
    """Return a frame of header, context row, heavy rule, ``body`` and keybar.

    Args:
        view: The render being built.
        crumb: The breadcrumb, without its leading gutter.
        ctx: The context row under the header.
        body: The body rows; a row not yet :class:`Fixed` is laid out here.
        keys: The keybar pairs.
    """
    w = view.w
    rows: list[str] = [header(view, " " + crumb), g_row(" " + ctx, w), bar(w)]
    rows.extend(row if isinstance(row, Fixed) else g_row(row, w) for row in body)
    return build(view, rows, keybar(keys, w))


def prose(name: str, sentences: Sequence[str], room: int) -> list[str]:
    """Return a labelled paragraph wrapped to ``room`` characters."""
    lines: list[str] = []
    cur = ""
    for word in " ".join(sentences).split(" "):
        if not cur:
            cur = word
        elif cell_len(f"{cur} {word}") <= room:
            cur += f" {word}"
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return [g_pad("", _GUTTER) + line if i else lab(name, line) for i, line in enumerate(lines)]


@dataclass(frozen=True, slots=True, kw_only=True)
class Scrollbar:
    """Where a card's window sits in its content.

    Attributes:
        total: The content's line count.
        take: The lines shown.
        start: The first line shown.
    """

    total: int
    take: int
    start: int


def boxed(
    view: View,
    *,
    crumb: str,
    ctx: str,
    pre: Sequence[str],
    title: str,
    lines: Sequence[str],
    foot: str | None,
    keys: Sequence[Pair],
    scrollbar: Scrollbar | None = None,
) -> list[str]:
    """Return a bordered card drawn over its own backdrop.

    Args:
        view: The render being built.
        crumb: The breadcrumb, without its leading gutter.
        ctx: The context row under the header.
        pre: Rows above the card.
        title: The card's title, set into its top border.
        lines: The card's content lines.
        foot: The row under the card, if any.
        keys: The keybar pairs.
        scrollbar: The window position; a card whose content is cut draws its thumb
            column inside the right border.
    """
    w = view.w
    body: list[str] = list(pre)
    sliced = scrollbar is not None and scrollbar.total > len(lines)
    top = f"┌─ {title} "
    body.append(top + RULE_THIN * max(0, w - cell_len(top) - 1) + "┐")
    for line in lines:
        body.append(f"│ {g_pad(line, w - 5)} █│" if sliced else f"│ {g_pad(line, w - 4)} │")
    body.append("└" + RULE_THIN * (w - 2) + "┘")
    if foot:
        body.append(" " + foot)
    return g_frame(view, crumb=crumb, ctx=ctx, body=body, keys=keys)


# ---------- the notification rack and the verbose row ----------


def toast_box(toast: Toast, w: int) -> list[str]:
    """Return a toast's three bordered rows, ``w`` cells wide.

    An error toast leads its title with the error kind's ``!``; the purged token ``✗`` is a
    value-column glyph and would read as a purged fact. Any other severity is carried by
    its title word and the border's colour alone. A toast with no title, such as the quit
    prompt, draws its top border unbroken.
    """
    glyph = "! " if toast.sev == Severity.ERR else ""
    head = glyph + toast.title
    top = "─" * (w - 2) if not head else "─ " + head + " " + "─" * max(1, w - 5 - cell_len(head))
    return [
        "┌" + top + "┐",
        "│ " + pad(clip_words(toast.text, w - 4), w - 4) + " │",
        "└" + "─" * (w - 2) + "┘",
    ]


def overlay_row(out: list[str], row: int, col: int, text: str) -> None:
    """Overwrite ``out[row]`` from column ``col``: the one place a row is painted over."""
    if row < 1 or row >= len(out):
        return
    base = out[row]
    out[row] = base[:col] + text + base[col + cell_len(text) :]


#: The gutter mark a record marked for a bulk verb carries, in the cell left of its key.
MARKED = "+"
_CARET = "▸"


def paint_marks(session: Session, out: list[str]) -> None:
    """Mark each body row that opens with a record marked for a bulk verb.

    The mark is text in the gutter cell left of the record's key, so a marked row says so
    in plain mode too, and on every route that lists the records the marks are taken from.

    Args:
        session: The session whose marks are painted.
        out: The body rows, painted in place; the header row is never a record's row.
    """
    if not session.marked:
        return
    marked = set(session.marked)
    for i in range(1, len(out)):
        row = out[i]
        # the leading blanks are one cell each, so their width is the key's string offset
        at = cell_len(row) - cell_len(row.lstrip(" "))
        if row[at : at + 1] == _CARET:
            at += 2
        if at and row[at - 1] == " " and row[at:].split(" ", 1)[0] in marked:
            out[i] = f"{row[: at - 1]}{MARKED}{row[at:]}"


def paint_rack(session: Session, out: list[str], w: int, *, verbose: bool) -> None:
    """Paint the standing toasts bottom-right over the body, newest lowest, one shared width.

    Args:
        session: The session whose toasts are painted.
        out: The body rows, painted in place.
        w: The frame width in cells.
        verbose: Whether the verbose row takes the last body row.
    """
    if not session.toasts:
        return
    width = 0
    for toast in session.toasts:
        width = max(width, 28, cell_len(toast.title) + 9, cell_len(toast.text) + 4)
    width = min(w - 2, width)
    bottom = len(out) - 1 - (1 if verbose else 0)
    for toast in reversed(session.toasts):
        top = bottom - 2
        if top < 1:
            break
        for offset, text in enumerate(toast_box(toast, width)):
            overlay_row(out, top + offset, w - 1 - width, text)
        bottom = top - 1


def paint_verbose(session: Session, out: list[str], w: int) -> None:
    """Paint the ``--verbose`` trace over the last body row."""
    trace = session.trace or "no keystroke yet"
    overlay_row(out, len(out) - 1, 0, pad(f" VERBOSE   {trace}", w))
