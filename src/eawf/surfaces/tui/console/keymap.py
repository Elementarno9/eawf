"""The key tables beside the route tables: the global grammar, overlays, drawers and entry.

The route tables live in :mod:`~eawf.surfaces.tui.console.keybar`. The global grammar is
:data:`GLOBAL_KEYS`, which help's EVERYWHERE block prints and the refusal gate admits, so
the keybar, the gate and help read one set of tables. An overlay accepts only its own keys
and a drawer owns its keys while it is open; ``-`` clears the rack under either, because
the rack is the console's rather than theirs. The entry layer lets its state's keys
through with a short allowlist. Keys are named the way the dispatcher matches them
(``ArrowDown``, ``Escape``, ``.``, ``Y``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import (
    KEY,
    RECORD_FRAME_KEYS,
    ROUTE_KEYS,
    KeyEntry,
    KeyKind,
    Pair,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session

ENTRY_ROUTE = "entry"
# The key that dismisses the rack, accepted by every overlay.
DISMISS = "-"

OVERLAY_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "help": ("PageUp", "PageDown", "Escape"),
        "palette": ("Escape",),
        "consequence": ("Enter", "Escape"),
        "question": ("1", "2", "3", "4", "w", "x", "Escape"),
        "pause": ("n", "c", "Escape"),
        "evidence": ("ArrowUp", "ArrowDown", "k", "j", "y", "Escape"),
        "acceptance": ("ArrowUp", "ArrowDown", "k", "j", "y", "Escape"),
        "readiness": ("ArrowUp", "ArrowDown", "k", "j", "Escape"),
        "resolution": ("Y", "Escape"),
        "draft": ("ArrowUp", "ArrowDown", "k", "j", "Enter", "p", "x", "Escape"),
        "marker": ("Escape",),
    }
)
DRAWER_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "go": (*REGISTRY.go_map, "Escape"),
        "actions": ("Escape", ".", *"abcdefghijklmnopqrstuvwxyz*"),
        "inspect": ("y", "Escape"),
        "raw": ("y", "Escape"),
    }
)
DRAWER_PAIRS: Mapping[str, tuple[Pair, ...]] = MappingProxyType(
    {
        "go": (("g …", "destination"), ("Esc", "cancel")),
        "actions": (("key", "run · consequence first"), ("Esc", "close")),
        "inspect": (("y", "copy"), ("Esc", "close")),
        "raw": (("y", "copy"), ("Esc", "close")),
    }
)
# The keys the entry layer lets through beside the state's own keys.
ENTRY_ALLOW: tuple[str, ...] = ("ArrowUp", "ArrowDown", "k", "j", "?", "Escape", "Enter", "/")


class Where(StrEnum):
    """Where a global key acts, which is where help teaches it.

    Members:
        ALWAYS: On every route.
        INSPECT: Where the route's table binds inspect.
        DEPTH: Where the route climbs through a record, so it has a containment parent
            and siblings at its depth.
        SELECTION: On a linked console, where the route lists lifecycle records a bulk
            preview can act on.
    """

    ALWAYS = "always"
    INSPECT = "inspect"
    DEPTH = "depth"
    SELECTION = "selection"


@dataclass(frozen=True, slots=True)
class GlobalKey:
    """One key of the global grammar, as help's EVERYWHERE block prints it.

    Attributes:
        token: The key as help prints it.
        text: What the key does.
        keys: The dispatcher key names it binds.
        where: Where it acts, and so where help lists it.
    """

    token: str
    text: str
    keys: tuple[str, ...]
    where: Where = Where.ALWAYS


# The global grammar. ``?`` is the key that opens help, so the card it opens does not list
# it; every other global is listed wherever it acts, dropped from a keybar or not.
GLOBAL_KEYS: tuple[GlobalKey, ...] = (
    GlobalKey("g …", "go-prefix · Esc or 1.5s cancels, visibly", ("g",)),
    GlobalKey("/", "command palette", ("/",)),
    GlobalKey(".", "action menu — disabled verbs stay visible, with reasons", (".",)),
    GlobalKey("Esc", "back · restores scope, row and filter exactly", ("Escape",)),
    GlobalKey("y", "copy the value · answers in the notification rack", ("y",)),
    GlobalKey("Y", "copy the stable URN", ("Y",)),
    GlobalKey("-", "dismiss the notifications — nothing else clears them", ("-",)),
    GlobalKey("!", "jump to your top attention item", ("!",)),
    GlobalKey("i", "inspect the focused field", ("i",), Where.INSPECT),
    GlobalKey("u", "up to the containment parent", ("u",), Where.DEPTH),
    GlobalKey("[ ]", "previous / next sibling at this depth", ("[", "]"), Where.DEPTH),
    GlobalKey("Space", "mark the row for one bulk preview", (" ",), Where.SELECTION),
    GlobalKey(",", "clear the marks", (",",), Where.SELECTION),
    GlobalKey("Ctrl+C", "quit · guarded, press again within 1.5s", ("ctrl+c",)),
)
# The routes that list lifecycle records a bulk preview can act on.
SELECTION_ROUTES: frozenset[str] = frozenset(
    {"scope.home", "track", "milestone", "batch.detail", "task.detail", "backlog", "activity"}
    | {"run.detail"}
)
# The keys a linked consequence card binds: confirm, cancel, walk its result rows and
# reconcile the unknown one.
CARD_KEYS: frozenset[str] = frozenset({"Enter", "Escape", "ArrowUp", "ArrowDown", "k", "j", "n"})
HELP_KEY = "?"
# The key the header's ``!N NEEDS YOU`` badge stands for: it jumps to the top attention
# item from any route, and help lists it among the global keys.
ATTENTION_JUMP_KEY = "!"
# Keys the dispatcher names but that act only as part of another key; every gate passes them.
MODIFIERS: frozenset[str] = frozenset({"Shift", "Control", "Alt", "Meta", "CapsLock"})
# Keys that move the cursor on every route whether or not its table prints them, and the
# aliases that stand for a key the tables do print.
MOTION: frozenset[str] = frozenset(
    {
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "PageUp",
        "PageDown",
        "Home",
        "End",
        "Enter",
        "Tab",
    }
)
#: The keys a route's native frame draws nothing for, so its bar neither advertises nor
#: binds them: no filter field for ``\`` to type into, no kind for ``k`` to cycle (``k``
#: also steps up), no detail beyond the docked repair readout, and no criteria or Runs
#: region on a Task frame for Tab to move between.
NATIVE_UNSERVED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "history": frozenset({"\\"}),
        "health": frozenset({"\\", "Enter"}),
        "search": frozenset({"\\", "k"}),
        "sandbox.log": frozenset({"\\"}),
        "task.detail": frozenset({"Tab"}),
    }
)
#: The routes whose Tab walks the groups or sections of the one record the frame is about,
#: so a frame drawn with no subject -- the route's plain register -- has nothing to cycle.
SUBJECT_TAB: frozenset[str] = frozenset({"track", "milestone"})


def unserved(route: str, subject: str | None) -> frozenset[str]:
    """Return the keys a native frame of ``route`` serves nothing for, given its subject."""
    keys = NATIVE_UNSERVED.get(route, frozenset())
    return keys | {"Tab"} if subject is None and route in SUBJECT_TAB else keys


ALIASES: Mapping[str, str] = MappingProxyType({"j": "ArrowDown", "k": "ArrowUp", "ctrl+f": "\\"})
ATTACH_LATER = KeyEntry("attach later", ("/",))


def entry_keys(session: Session, fixture: Fixture) -> tuple[KeyEntry, ...]:
    """Return the entry layer's key table: the state's own keys, then ``/ attach later``."""
    states = fixture.proto.entry
    state = states[session.entry_sel] if session.entry_sel < len(states) else states[0]
    own = [KeyEntry(label, tuple(key.split())) for key, label in state.keys]
    return (*own, ATTACH_LATER)


def route_keys(
    session: Session, fixture: Fixture, route: str | None = None
) -> tuple[KeyEntry, ...]:
    """Return the key table of ``route``, the session's route by default."""
    target = route or session.route
    if target == ENTRY_ROUTE:
        return entry_keys(session, fixture)
    return ROUTE_KEYS.get(target, ())


#: The keys a route's native frame serves beyond its table: scope home's inspect opens the
#: scope drawer, which names the project, its root id and the tree's URN -- facts only a
#: tree read through a daemon link states, so the prototype replay's bar never shows it.
NATIVE_SERVED: Mapping[str, tuple[KeyEntry, ...]] = MappingProxyType(
    {"scope.home": (KEY["inspect"],)}
)


def native_keys(route: str, *, windowed: bool) -> tuple[KeyEntry, ...]:
    """Return the key table a native frame of ``route`` advertises.

    A native table can outgrow any screen, so a frame whose table was cut to its window
    pages as well as steps: the page and ends keys follow the route's own verbs, ahead of
    the globals. A frame showing every row has nowhere to page to and does not offer it.

    Args:
        route: The route whose table is read.
        windowed: Whether a table on the frame hides rows beyond its window.

    Raises:
        KeyError: ``route`` has no key table.
    """
    paging = (KEY["page"], KEY["ends"])
    table = tuple(entry for entry in ROUTE_KEYS[route] if entry not in paging)
    served = NATIVE_SERVED.get(route, ())
    if served:
        # a served key sits beside the menu, where the route tables keep the inspect pair
        menu = table.index(KEY["actions"]) + 1 if KEY["actions"] in table else len(table)
        table = (*table[:menu], *served, *table[menu:])
    if not windowed:
        return table
    at = next((i for i, entry in enumerate(table) if entry.kind is KeyKind.GLOBAL), len(table))
    return (*table[:at], *paging, *table[at:])


def can(session: Session, fixture: Fixture, entry: KeyEntry) -> bool:
    """Return whether the frame on screen advertises ``entry``'s key.

    A record frame draws its own bar in place of the route's, so a key that bar offers
    acts there whether or not the route's own table carries it.
    """
    record = RECORD_FRAME_KEYS if session.record_facts is not None else ()
    served = () if fixture.prototype else NATIVE_SERVED.get(session.route, ())
    return any(e.token == entry.token for e in (*route_keys(session, fixture), *record, *served))


def acts_here(key: GlobalKey, route: str, *, linked: bool = False) -> bool:
    """Return whether global ``key`` acts on ``route``, and so whether help teaches it there.

    Args:
        key: The global key.
        route: The route the operator stands on.
        linked: Whether the console holds a daemon link, which the selection keys need.
    """
    if key.where is Where.SELECTION:
        return linked and route in SELECTION_ROUTES
    if key.where is Where.INSPECT:
        served = NATIVE_SERVED.get(route, ()) if linked else ()
        return any(KEY["inspect"].keys == e.keys for e in (*ROUTE_KEYS.get(route, ()), *served))
    if key.where is Where.DEPTH:
        escape = REGISTRY.escapes.get(route)
        return escape is not None and escape.via is not None
    return True


def allowlist(session: Session, fixture: Fixture) -> frozenset[str]:
    """Return every key the active surface consumes; the refusal gate refuses the rest.

    With nothing open, that is the route's table, the global grammar, the motion keys and
    each alias of a key the set already holds.
    """
    overlay = session.overlay
    if overlay == "consequence" and session.mutation is not None:
        return CARD_KEYS | {DISMISS}
    if overlay is not None:
        if overlay in OVERLAY_KEYS:
            return frozenset(OVERLAY_KEYS[overlay]) | {DISMISS}
        if overlay in DRAWER_KEYS:
            return frozenset(DRAWER_KEYS[overlay]) | {DISMISS}
    if session.prefix == "g":
        return frozenset(DRAWER_KEYS["go"])
    keys: set[str] = {key for entry in route_keys(session, fixture) for key in entry.keys}
    keys.update(key for global_key in GLOBAL_KEYS for key in global_key.keys)
    keys.update(MOTION | MODIFIERS)
    keys.add(HELP_KEY)
    keys.update(alias for alias, key in ALIASES.items() if key in keys)
    return frozenset(keys)
