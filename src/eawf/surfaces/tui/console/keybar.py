"""The keybar: the one key vocabulary, the per-route key tables and the width-bounded bar.

A key is declared by the names the dispatcher matches (``ArrowUp``, ``PageDown``,
``Escape``), and the keybar token is derived from those names, so the bar always spells a
key in full (``PageUp PageDown``, ``Home End``) and names a pair of direction keys once
(``↑↓``). A token that abbreviates a key or slashes two keys together is refused before it
renders, and so is a shifted key other than ``?`` and ``Y``: no case convention may encode
danger. The bar is one line: a one-cell margin, then ``<key> <label>`` pairs three cells
apart. A route verb never drops; the global pairs drop in one fixed order until the pairs
fit the frame's width budget, so the bar is the route's whole table plus as much of the
global set as the width allows.

Every route frame draws its bar from :data:`ROUTE_KEYS`, and the help overlay and the
dispatcher's refusal gate read the same tables, so a binding added here reaches all three.
The tables are audited when this module loads: a letter with two meanings inside one route,
or a reserved global key bound to a route-local verb, refuses to import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from eawf.surfaces.tui.console.registry import REGISTRY, RouteRegistry
from eawf.surfaces.tui.console.width import cell_len, pad

Pair = tuple[str, str]

# Cells before the first pair, and after the last one at a bar that fills its budget.
MARGIN = 1
GAP = "   "

# Display names for keys whose dispatcher name is not what the bar prints; every other key
# prints as its own full name.
KEY_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "ArrowUp": "↑",
        "ArrowDown": "↓",
        "ArrowLeft": "←",
        "ArrowRight": "→",
        "Escape": "Esc",
    }
)
_ARROWS = frozenset("↑↓←→")

# Abbreviated key names the bar refuses, each with the full name it must spell instead.
ABBREVIATED: Mapping[str, str] = MappingProxyType(
    {
        "PgUp": "PageUp",
        "PgDn": "PageDown",
        "PgDown": "PageDown",
        "Bksp": "Backspace",
        "Del": "Delete",
        "Ins": "Insert",
    }
)


def assert_full_key_names(token: str) -> None:
    """Refuse a keybar token that abbreviates a key or slashes two keys together.

    Raises:
        ValueError: ``token`` is blank, holds an abbreviated key name, or holds a slashed
            pair such as ``↑/↓``; a lone ``/`` is the palette key and passes.
    """
    words = token.split()
    if not words:
        raise ValueError("a keybar token names at least one key")
    for word in words:
        full = ABBREVIATED.get(word)
        if full is not None:
            raise ValueError(f"keybar token {token!r} abbreviates {full} as {word}")
        if "/" in word and word != "/":
            raise ValueError(f"keybar token {token!r} is a slashed pair; name the verb once")


# Single keys that need Shift on a US layout, beside the capital letters.
_SHIFTED_SYMBOLS = frozenset('~!@#$%^&*()_+{}|:"<>?')
# The only shifted keys a frame may show: help is chrome rather than a verb, and ``Y``
# copies the stable URN, so neither mutates anything.
ADMITTED_SHIFTED = frozenset({"?", "Y"})


def is_shifted(key: str) -> bool:
    """Return whether ``key`` is a single key that needs Shift: a capital or a symbol."""
    return len(key) == 1 and (key.isupper() or key in _SHIFTED_SYMBOLS)


def key_token(keys: Sequence[str]) -> str:
    """Return the keybar token for the keys one entry binds.

    Adjacent arrow keys join into one glyph pair (``↑↓``); every other key is separated by
    a space and printed under its full name.

    Raises:
        ValueError: ``keys`` is empty, or a key name is abbreviated.
    """
    if not keys:
        raise ValueError("a key entry binds at least one key")
    names = [KEY_NAMES.get(key, key) for key in keys]
    token = names[0]
    for before, name in pairwise(names):
        token += name if before in _ARROWS and name in _ARROWS else f" {name}"
    assert_full_key_names(token)
    return token


class KeyKind(StrEnum):
    """What a binding does, which decides where it may sit and whether it previews.

    Members:
        NAV: Moves the view, the cursor or the focus, or opens a surface.
        GLOBAL: Part of the global grammar; advertised only where the width allows.
        SAFE: A frequent local verb that is reversible and takes no preview.
        ANSWER: Resolves a question or a delivery disposition and mutates no lifecycle.
        PRIMARY: The route's one direct mutation; it opens the consequence preview first.
    """

    NAV = "nav"
    GLOBAL = "global"
    SAFE = "safe"
    ANSWER = "answer"
    PRIMARY = "primary"


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """One advertised binding: the verb's label and the keys that perform it.

    Attributes:
        label: What the keys do where the bar shows them, never the destination's name.
        keys: The dispatcher key names, in the order the token prints them.
        kind: What the binding does; a navigation key unless declared otherwise.

    Raises:
        ValueError: ``label`` is blank, ``keys`` fails :func:`key_token`, or a key is
            shifted and is not one of :data:`ADMITTED_SHIFTED`.
    """

    label: str
    keys: tuple[str, ...]
    kind: KeyKind = KeyKind.NAV

    def __post_init__(self) -> None:
        """Validate the label and the keys once, at declaration."""
        if not self.label.strip():
            raise ValueError(f"key entry {self.keys!r} needs a label")
        key_token(self.keys)
        for key in self.keys:
            if is_shifted(key) and key not in ADMITTED_SHIFTED:
                raise ValueError(f"key entry {self.label!r} binds shifted key {key!r}")

    @property
    def token(self) -> str:
        """Return the key names as the bar prints them."""
        return key_token(self.keys)

    def pair(self) -> Pair:
        """Return the ``(token, label)`` pair the bar composes."""
        return (self.token, self.label)


_UP_DOWN = ("ArrowUp", "ArrowDown")


def _k(label: str, *keys: str, kind: KeyKind = KeyKind.NAV) -> KeyEntry:
    """Return an entry; a shorthand for the tables below."""
    return KeyEntry(label, keys, kind)


_G = KeyKind.GLOBAL

# The shared vocabulary. A key the global grammar reserves is bound only through one of
# these entries, never through a route-local one, so it means the same thing everywhere.
KEY: Mapping[str, KeyEntry] = MappingProxyType(
    {
        "up": _k("row", *_UP_DOWN),
        "up_event": _k("event", *_UP_DOWN),
        "page": _k("page", "PageUp", "PageDown"),
        "ends": _k("ends", "Home", "End"),
        "enter": _k("drill", "Enter"),
        "open": _k("open", "Enter"),
        "esc": _k("back", "Escape", kind=_G),
        "clear_bucket": _k("clear bucket", "Escape", kind=_G),
        "tab": _k("buckets", "Tab"),
        "tab_section": _k("section", "Tab"),
        "go": _k("go", "g", kind=_G),
        "palette": _k("palette", "/", kind=_G),
        "filter": _k("filter", "\\"),
        "refine": _k("refine", "\\"),
        "actions": _k("actions", ".", kind=_G),
        "help": _k("help", "?", kind=_G),
        "inspect": _k("inspect", "i", kind=_G),
        "stack": _k("stack", "i", kind=_G),
        "raw": _k("raw", "r", kind=KeyKind.SAFE),
        "transcript": _k("transcript", "Enter"),
        "copy": _k("copy", "y", kind=_G),
        "digest": _k("copy digest", "y", kind=_G),
        "answer": _k("answer", "a", kind=KeyKind.ANSWER),
        "deny": _k("deny", "x", kind=KeyKind.ANSWER),
        "snooze": _k("snooze", "z", kind=KeyKind.ANSWER),
        "resolve": _k("resolve", "v", kind=KeyKind.PRIMARY),
    }
)

_K = KEY

# The keys a record frame offers after its row keys, in place of its route's own table.
RECORD_FRAME_KEYS: tuple[KeyEntry, ...] = (KEY["actions"], KEY["inspect"], KEY["esc"])

# Every route's ordered key table, route verbs first and globals last; the pre-session
# ``entry`` layer builds its own from the state it is in, so it has no row here. A frame
# that shows a state-dependent part of its table picks the entries by label with
# :func:`pick`, so it can leave keys out but never spell one the table lacks.
ROUTE_KEYS: Mapping[str, tuple[KeyEntry, ...]] = MappingProxyType(
    {
        "scope.home": (
            _k("tree", *_UP_DOWN),
            _K["enter"],
            _k("list", "Tab"),
            _K["go"],
            _K["actions"],
            _K["help"],
        ),
        "activity": (_K["up"], _K["page"], _K["enter"], _K["tab"], _K["filter"], _K["actions"]),
        "run.detail": (_K["up_event"], _K["actions"], _K["raw"], _K["inspect"], _K["esc"]),
        "attention": (
            _K["up"],
            _K["open"],
            _K["tab"],
            _K["answer"],
            _K["deny"],
            _K["snooze"],
            _K["resolve"],
            _K["actions"],
            _K["esc"],
        ),
        "batch.detail": (_K["up"], _K["enter"], _K["actions"], _K["inspect"], _K["esc"]),
        "milestone": (
            _K["up"],
            _K["enter"],
            _K["tab_section"],
            _K["actions"],
            _K["digest"],
            _K["esc"],
        ),
        "track": (
            _K["up"],
            _k("group", "Tab"),
            _K["enter"],
            _K["actions"],
            _K["inspect"],
            _K["esc"],
        ),
        "task.detail": (_K["up"], _K["enter"], _K["actions"], _K["inspect"], _K["esc"]),
        "release": (
            _K["up"],
            _K["tab_section"],
            _K["enter"],
            _k("readiness", "m"),
            _K["actions"],
            _K["esc"],
        ),
        "timeline": (
            _K["up"],
            _k("marker", "ArrowLeft", "ArrowRight"),
            _K["tab_section"],
            _K["enter"],
            _K["actions"],
            _K["esc"],
        ),
        # Enter opens the draft card; promoting is the card's verb, never the route's
        "backlog": (
            _K["up"],
            _k("group", "Tab"),
            _K["open"],
            _K["palette"],
            _K["esc"],
        ),
        "campaign": (
            _K["up"],
            _K["tab_section"],
            _K["open"],
            _K["actions"],
            _K["inspect"],
            _K["esc"],
        ),
        "history": (_K["up"], _K["filter"], _K["enter"], _K["copy"], _K["esc"]),
        "settings": (
            _k("field", *_UP_DOWN),
            _K["tab_section"],
            _k("edit", "Enter"),
            _k("layer", "l"),
            _k("unset", "x", kind=KeyKind.PRIMARY),
            _K["esc"],
            _K["stack"],
            _K["filter"],
        ),
        "trust": (
            _k("field", *_UP_DOWN),
            _k("evidence", "Enter"),
            _K["actions"],
            _K["inspect"],
            _K["esc"],
        ),
        "evidence": (
            _k("rung", *_UP_DOWN),
            _k("what it found", "Enter"),
            _K["copy"],
            _K["esc"],
        ),
        "evidence.digest": (_K["copy"], _k("close", "Escape", kind=_G)),
        "health": (
            _k("check", *_UP_DOWN),
            _k("detail", "Enter"),
            _K["filter"],
            _K["esc"],
        ),
        "sandbox.log": (
            _K["up"],
            _k("run", "Enter"),
            _K["filter"],
            _k("policy", "p"),
            _K["esc"],
        ),
        "unattended": (
            _K["up"],
            _k("run", "Enter"),
            _k("request pause", "a", kind=KeyKind.PRIMARY),
            # drain is the route's second write, so it lives in the action menu: a route
            # binds one direct mutation key
            _K["actions"],
            _K["esc"],
        ),
        "search": (
            _k("hit", *_UP_DOWN),
            _K["enter"],
            _K["refine"],
            _k("kind", "k"),
            _K["esc"],
        ),
        "transcript": (
            _k("block", *_UP_DOWN),
            _k("fold", "Enter"),
            _k("follow", "f", kind=KeyKind.SAFE),
            _K["copy"],
            _K["esc"],
        ),
        "receipt": (_K["copy"], _K["esc"]),
        "cost.ceiling": (_K["up"], _k("run", "Enter"), _K["esc"]),
        "crash.recovery": (
            _k("door", *_UP_DOWN),
            _k("choose", "Enter"),
            _K["inspect"],
            _k("later", "Escape", kind=_G),
        ),
        "git.pr": (
            _K["up"],
            _k("commit", "Enter"),
            _k("conflict", "m"),
            _K["copy"],
            _K["esc"],
        ),
        "history.diff": (
            _k("field", *_UP_DOWN),
            _k("field", "Enter"),
            _k("entity", "e"),
            _k("revisions", "p"),
            _K["esc"],
        ),
        "settings.stack": (_k("layer", *_UP_DOWN), _k("close", "Escape", kind=_G)),
        "notifications": (_k("class", *_UP_DOWN), _k("close", "Escape", kind=_G)),
        "merge.conflict": (
            _k("hunk", *_UP_DOWN),
            _K["copy"],
            _k("close", "Escape", kind=_G),
        ),
        "export": (
            _k("part", *_UP_DOWN),
            _k("export", "Enter"),
            _k("cancel", "Escape", kind=_G),
        ),
        "campaign.step": (
            _k("region", "Tab"),
            _k("line", *_UP_DOWN),
            _k("product", *_UP_DOWN),
            _K["open"],
            _K["copy"],
            _K["esc"],
        ),
        "campaign.artifact": (
            _k("scroll", *_UP_DOWN),
            _K["copy"],
            _k("close", "Escape", kind=_G),
        ),
    }
)


def unregistered_routes(
    tables: Mapping[str, Sequence[KeyEntry]], *, registry: RouteRegistry = REGISTRY
) -> tuple[str, ...]:
    """Return the routes ``tables`` keys that the registry does not hold, sorted."""
    return tuple(sorted(set(tables) - set(registry.ids)))


_UNKEYED = unregistered_routes(ROUTE_KEYS)
if _UNKEYED:
    raise ValueError(f"key tables for unregistered routes: {', '.join(_UNKEYED)}")

# The global grammar's keys: a route table binds each only through the shared vocabulary,
# and a route wanting a verb of its own takes an unclaimed letter instead.
RESERVED: frozenset[str] = frozenset("g/\\.?!iryY-u[]")
_SHARED: frozenset[KeyEntry] = frozenset(KEY.values())


def collision_audit(tables: Mapping[str, Sequence[KeyEntry]]) -> tuple[str, ...]:
    """Return each collision in ``tables``, one line per finding, in route order.

    Two findings exist: a single key (a letter or a symbol) that carries two labels inside
    one route, and a reserved key bound by a route-local entry rather than the shared
    vocabulary. A named key such as ``Enter`` or the arrows may change its label with the
    state a route is in, so only single keys are held to one meaning.
    """
    found: list[str] = []
    for route in sorted(tables):
        meaning: dict[str, str] = {}
        for entry in tables[route]:
            for key in entry.keys:
                if len(key) != 1:
                    continue
                if key in RESERVED and entry not in _SHARED:
                    found.append(f"{route}: reserved key {key!r} bound to {entry.label!r}")
                held = meaning.setdefault(key, entry.label)
                if held != entry.label:
                    found.append(f"{route}: {key!r} means both {held!r} and {entry.label!r}")
    return tuple(found)


_COLLISIONS = collision_audit(ROUTE_KEYS)
if _COLLISIONS:
    raise ValueError(f"key table collisions: {'; '.join(_COLLISIONS)}")


def route_pairs(route: str) -> tuple[Pair, ...]:
    """Return ``route``'s whole table as keybar pairs, in advertising order.

    Raises:
        KeyError: ``route`` has no key table.
    """
    return tuple(entry.pair() for entry in ROUTE_KEYS[route])


def pick(route: str, *labels: str) -> list[Pair]:
    """Return the pairs of ``route``'s entries labelled ``labels``, in the order given.

    A frame whose bar follows its state names the entries it shows here, so a label the
    table does not hold fails at render rather than drawing a key nothing binds.

    Raises:
        KeyError: ``route`` has no key table, or no entry in it carries one of ``labels``.
    """
    table = {entry.label: entry for entry in ROUTE_KEYS[route]}
    missing = [label for label in labels if label not in table]
    if missing:
        raise KeyError(f"{route} has no key labelled {', '.join(missing)}")
    return [table[label].pair() for label in labels]


def budget(w: int) -> int:
    """Return the cells a keybar's margin and pairs may fill in a frame ``w`` cells wide.

    The bar keeps one cell clear at each edge, so the budget is ``w - 1`` counting the
    leading margin.

    Raises:
        ValueError: ``w`` leaves no room for the margins.
    """
    if w < 2 * MARGIN:
        raise ValueError(f"a keybar needs at least {2 * MARGIN} cells, got {w}")
    return w - MARGIN


def _compose(pairs: Sequence[Pair]) -> str:
    """Return the margin and the pairs, unpadded."""
    return " " * MARGIN + GAP.join(f"{token} {label}" for token, label in pairs)


# The global pairs in the order they leave a bar too wide for its budget; the copy,
# dismiss and inspect pairs go after these, from the tail.
GLOBAL_DROP_ORDER: tuple[str, ...] = ("?", "/", ".", "Esc", "g")
#: The help pair's token, which a frame drawn from a read model pins last.
HELP_TOKEN = GLOBAL_DROP_ORDER[0]
GLOBAL_TOKENS: frozenset[str] = frozenset({*GLOBAL_DROP_ORDER, "-", "y", "Y", "i"})

# The paging pairs only repeat what the arrows do a screen at a time, so a bar that would
# otherwise lose the way back or a copy gives up the ends pair, then the pages pair, first.
PAGING_DROP_ORDER: tuple[str, ...] = ("Home End", "PageUp PageDown")

# The globals a bar gives up freely; any other global is worth a paging pair.
_CHEAP_GLOBALS: frozenset[str] = frozenset(GLOBAL_DROP_ORDER[: GLOBAL_DROP_ORDER.index("Esc")])


def _drop_at(pairs: Sequence[Pair]) -> int:
    """Return the index of the pair that leaves a bar too wide next.

    The global pairs leave in :data:`GLOBAL_DROP_ORDER`, then the other globals from the
    tail. A bar holding no global has only route verbs left, which the key-table contract
    keeps inside every budget, so its last pair goes only for a table that breaks it. The
    first pair is never chosen.
    """
    last = len(GLOBAL_DROP_ORDER)
    ranked = [
        (GLOBAL_DROP_ORDER.index(token) if token in GLOBAL_DROP_ORDER else last, -i)
        for i, (token, _label) in enumerate(pairs)
        if i and token in GLOBAL_TOKENS
    ]
    return -min(ranked)[1] if ranked else len(pairs) - 1


def keybar(pairs: Sequence[Pair], w: int, *, keep_actions: bool = False) -> str:
    """Return the keybar row: exactly ``w`` cells, global pairs dropped past the budget.

    The first pair never drops; a lone pair wider than the frame is clipped instead.

    Args:
        pairs: ``(token, label)`` pairs in advertising order, globals last.
        w: The frame width in cells.
        keep_actions: Whether the action menu's pair is worth the paging pairs. A frame
            drawn from a read model keeps it, because its menu is where the verbs of the
            record on screen are refused or offered; the prototype replay drops it first,
            as its tracked bars do. Such a frame also pins its help pair last, so the one
            key that explains every other is never the one a narrow terminal loses.

    Raises:
        ValueError: a token fails :func:`assert_full_key_names`, or ``w`` fails :func:`budget`.
    """
    for token, _label in pairs:
        assert_full_key_names(token)
    room = budget(w)
    cheap = _CHEAP_GLOBALS - {KEY["actions"].token} if keep_actions else _CHEAP_GLOBALS
    pinned = [p for p in pairs if p[0] == HELP_TOKEN] if keep_actions else []
    kept = [p for p in pairs if p not in pinned]
    if pinned and kept:
        room -= cell_len(GAP + _compose(pinned).lstrip())
    bar = _compose(kept)
    while cell_len(bar) > room and len(kept) > 1:
        at = _drop_at(kept)
        token = kept[at][0]
        spared = (
            _without_paging(kept, room) if token in GLOBAL_TOKENS and token not in cheap else None
        )
        if spared is not None:
            kept = spared
            break
        del kept[at]
        bar = _compose(kept)
    return pad(_compose([*kept, *pinned]), w)


def _without_paging(pairs: Sequence[Pair], room: int) -> list[Pair] | None:
    """Return ``pairs`` less the fewest paging pairs that fit ``room``, or ``None``.

    ``None`` when dropping every paging pair still leaves the bar too wide, so the pages
    are kept for a bar that would lose its global anyway.
    """
    trial = list(pairs)
    for token in PAGING_DROP_ORDER:
        trial = [pair for pair in trial if pair[0] != token]
        if cell_len(_compose(trial)) <= room:
            return trial
    return None


def route_bar(route: str, w: int) -> str:
    """Return ``route``'s keybar from its key table.

    Raises:
        KeyError: ``route`` has no key table.
        ValueError: ``w`` fails :func:`budget`.
    """
    return keybar([entry.pair() for entry in ROUTE_KEYS[route]], w)
