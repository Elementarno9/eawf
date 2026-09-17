"""The keybar: the one key vocabulary, the per-route key tables and the width-bounded bar.

A key is declared by the names the dispatcher matches (``ArrowUp``, ``PageDown``,
``Escape``), and the keybar token is derived from those names, so the bar always spells a
key in full (``PageUp PageDown``, ``Home End``) and names a pair of direction keys once
(``↑↓``). A token that abbreviates a key or slashes two keys together is refused before it
renders. The bar is one line: a one-cell margin, then ``<key> <label>`` pairs three cells
apart, and trailing pairs drop until the pairs fit the frame's width budget. Each route
table is ordered route verbs first and globals last, so the tail that drops is always a
global.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """One advertised binding: the verb's label and the keys that perform it.

    Attributes:
        label: What the keys do where the bar shows them, never the destination's name.
        keys: The dispatcher key names, in the order the token prints them.

    Raises:
        ValueError: ``label`` is blank, or ``keys`` fails :func:`key_token`.
    """

    label: str
    keys: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate the label and the keys once, at declaration."""
        if not self.label.strip():
            raise ValueError(f"key entry {self.keys!r} needs a label")
        key_token(self.keys)

    @property
    def token(self) -> str:
        """Return the key names as the bar prints them."""
        return key_token(self.keys)

    def pair(self) -> Pair:
        """Return the ``(token, label)`` pair the bar composes."""
        return (self.token, self.label)


_UP_DOWN = ("ArrowUp", "ArrowDown")


def _k(label: str, *keys: str) -> KeyEntry:
    """Return an entry; a shorthand for the tables below."""
    return KeyEntry(label, keys)


KEY: Mapping[str, KeyEntry] = MappingProxyType(
    {
        "up": _k("row", *_UP_DOWN),
        "up_event": _k("event", *_UP_DOWN),
        "page": _k("page", "PageUp", "PageDown"),
        "ends": _k("ends", "Home", "End"),
        "enter": _k("drill", "Enter"),
        "open": _k("open", "Enter"),
        "esc": _k("back", "Escape"),
        "tab": _k("buckets", "Tab"),
        "tab_section": _k("section", "Tab"),
        "go": _k("go", "g"),
        "filter": _k("filter", "\\"),
        "actions": _k("actions", "."),
        "help": _k("help", "?"),
        "inspect": _k("inspect", "i"),
        "raw": _k("raw", "r"),
        "copy": _k("copy", "y"),
        "digest": _k("copy digest", "y"),
        "answer": _k("answer", "a"),
        "deny": _k("deny", "x"),
        "snooze": _k("snooze", "z"),
        "resolve": _k("resolve", "v"),
    }
)

_K = KEY

# Every route's ordered key table; the pre-session ``entry`` layer builds its own from the
# state it is in, so it has no row here.
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
        "backlog": (_K["up"], _K["enter"], _K["esc"]),
        "campaign": (
            _K["up"],
            _K["tab_section"],
            _K["enter"],
            _K["actions"],
            _K["inspect"],
            _K["esc"],
        ),
        "history": (_K["up"], _K["filter"], _K["enter"], _K["copy"], _K["esc"]),
        "settings": (_K["up"], _K["enter"], _K["filter"], _K["inspect"], _K["esc"]),
        "trust": (_K["up"], _K["enter"], _K["actions"], _K["inspect"], _K["esc"]),
        "evidence": (_K["up"], _K["enter"], _K["copy"], _K["esc"]),
        "evidence.digest": (_K["copy"], _K["esc"]),
        "health": (_K["up"], _K["enter"], _K["filter"], _K["esc"]),
        "sandbox.log": (_K["up"], _K["enter"], _K["filter"], _k("policy", "p"), _K["esc"]),
        "unattended": (_K["up"], _K["enter"], _K["esc"]),
        "search": (_K["up"], _K["enter"], _K["filter"], _K["esc"]),
        "transcript": (_K["up"], _K["enter"], _k("follow", "f"), _K["copy"], _K["esc"]),
        "receipt": (_K["copy"], _K["esc"]),
        "cost.ceiling": (_K["up"], _K["enter"], _K["esc"]),
        "crash.recovery": (_K["up"], _K["enter"], _K["inspect"], _K["esc"]),
        "git.pr": (_K["up"], _K["enter"], _K["copy"], _K["esc"]),
        "history.diff": (_K["up"], _K["enter"], _K["esc"]),
        "settings.stack": (_K["up"], _K["esc"]),
        "notifications": (_K["up"], _K["esc"]),
        "merge.conflict": (_K["up"], _K["copy"], _K["esc"]),
        "export": (_K["up"], _K["enter"], _K["esc"]),
        "campaign.step": (_K["up"], _K["enter"], _K["copy"], _K["esc"]),
        "campaign.artifact": (_k("scroll", *_UP_DOWN), _K["copy"], _K["esc"]),
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


def keybar(pairs: Sequence[Pair], w: int) -> str:
    """Return the keybar row: exactly ``w`` cells, trailing pairs dropped past the budget.

    The first pair never drops; a lone pair wider than the frame is clipped instead.

    Args:
        pairs: ``(token, label)`` pairs in advertising order, globals last.
        w: The frame width in cells.

    Raises:
        ValueError: a token fails :func:`assert_full_key_names`, or ``w`` fails :func:`budget`.
    """
    for token, _label in pairs:
        assert_full_key_names(token)
    room = budget(w)
    kept = list(pairs)
    bar = _compose(kept)
    while cell_len(bar) > room and len(kept) > 1:
        kept.pop()
        bar = _compose(kept)
    return pad(bar, w)


def route_bar(route: str, w: int) -> str:
    """Return ``route``'s keybar from its key table.

    Raises:
        KeyError: ``route`` has no key table.
        ValueError: ``w`` fails :func:`budget`.
    """
    return keybar([entry.pair() for entry in ROUTE_KEYS[route]], w)
