"""The key tables beside the route tables: overlays, drawers, the entry layer and help.

The route tables live in :mod:`~eawf.surfaces.tui.console.keybar`. An overlay accepts only
its own keys (``-`` always passes), a drawer owns its keys while it is open, and the entry
layer lets its state's keys through with a short allowlist. Keys are named the way the
dispatcher matches them (``ArrowDown``, ``Escape``, ``.``, ``Y``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, KeyEntry, Pair
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session

ENTRY_ROUTE = "entry"
# The key that dismisses the rack, accepted by every overlay.
DISMISS = "-"

OVERLAY_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "help": ("Escape",),
        "palette": ("Escape",),
        "consequence": ("Enter", "Escape"),
        "question": ("1", "2", "3", "x", "Escape"),
        "pause": ("n", "c", "Escape"),
        "evidence": ("ArrowUp", "ArrowDown", "k", "j", "y", "Escape"),
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
        "inspect": ("y", "Escape", "Enter"),
        "raw": ("y", "Escape", "Enter"),
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
# Help's EVERYWHERE block: the token, what it does, and the key it binds.
GLOBAL_HELP: tuple[tuple[str, str, str], ...] = (
    ("g …", "go-prefix · Esc or 1.5s cancels, visibly", "g"),
    ("/", "command palette", "/"),
    (".", "action menu — disabled verbs stay visible, with reasons", "."),
    ("Esc", "back · restores scope, row and filter exactly", "Escape"),
    ("y", "copy the value · answers in the notification rack", "y"),
    ("Y", "copy the stable URN", "Y"),
    ("-", "dismiss the notifications — nothing else clears them", "-"),
)
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


def can(session: Session, fixture: Fixture, entry: KeyEntry) -> bool:
    """Return whether the session's route advertises ``entry``'s key."""
    return any(e.token == entry.token for e in route_keys(session, fixture))


def allowlist(session: Session, fixture: Fixture) -> frozenset[str]:
    """Return every key the active surface consumes; the dispatcher ignores the rest."""
    overlay = session.overlay
    if overlay is not None:
        if overlay in OVERLAY_KEYS:
            return frozenset(OVERLAY_KEYS[overlay]) | {DISMISS}
        if overlay in DRAWER_KEYS:
            return frozenset(DRAWER_KEYS[overlay])
    if session.prefix == "g":
        return frozenset(DRAWER_KEYS["go"])
    keys: set[str] = {key for entry in route_keys(session, fixture) for key in entry.keys}
    keys.update(key for _token, _text, key in GLOBAL_HELP)
    keys.update({"?", "Escape", "j", "k"})
    return frozenset(keys)
