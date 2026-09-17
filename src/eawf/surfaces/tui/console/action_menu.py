"""The action menu: every verb a route binds, safest first, refused ones with their reasons.

The menu is the capability-disclosure surface. Light verbs (open a surface, fill the
clipboard) come first and answer in the rack; heavy verbs (any write) follow and open
their consequence card. A verb the operator cannot use stays listed with its reason. The
menu is letter-driven: it draws no caret and its keybar promises neither arrows nor Enter.
One ordering feeds the rows and the letter dispatch, and :func:`outcome` is the one place
a pressed verb is judged, so the menu and the key path cannot disagree. The menus are
checked against the route registry when they are built: a menu belongs to a registered
route, a letter means one verb on its route, and a light verb opens only a route the
registry gives that route a door to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from eawf.surfaces.tui.console.keybar import KeyEntry, Pair
from eawf.surfaces.tui.console.registry import REGISTRY, DoorKind, RouteRegistry
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.width import cell_len, clip_words, pad

ACTIONS_OVERLAY = "actions"
PAIRS: tuple[Pair, ...] = (
    ("key", "run · consequence first"),
    KeyEntry("close", ("Escape",)).pair(),
)
NO_ACTIONS_TITLE = "NO ACTIONS"
NO_ACTIONS_TEXT = "No action is available here."
EMPTY_ROW = "   No verb is defined for this route."
# Column widths of the menu table after its two-cell gutter; the reason takes the rest.
TITLE_COLUMN = 10
KEY_COLUMN = 5
VERB_COLUMN = 21
_LEAD = "   "
_REASON_AT = cell_len(_LEAD) + TITLE_COLUMN + KEY_COLUMN + VERB_COLUMN


class VerbWeight(StrEnum):
    """How much a verb does: a light verb acts at once, a heavy one previews first."""

    LIGHT = "light"
    HEAVY = "heavy"


@dataclass(frozen=True, slots=True, kw_only=True)
class MenuVerb:
    """One verb in a route's action menu.

    Attributes:
        key: The menu-local letter that runs it.
        verb: The verb's name, as the menu row shows it.
        available: Whether the verb can act on the record at all.
        reason: Why it cannot, naming the evidence; empty when it can.
        authority: The authority class a write needs; empty for a verb that writes nothing.
        effects: What the consequence card says the verb does.
        non_effects: What the consequence card says it does not do.
        weight: Whether it acts at once or previews its consequence.
        target: The route a light verb opens; ``None`` for one that fills the clipboard.

    Raises:
        ValueError: ``key`` is not one character, ``verb`` is blank or wider than its
            column, an unavailable verb has no reason or an available one has one, or a
            heavy verb names a target.
    """

    key: str
    verb: str
    available: bool
    reason: str = ""
    authority: str = ""
    effects: str = ""
    non_effects: str = ""
    weight: VerbWeight = VerbWeight.HEAVY
    target: str | None = None

    def __post_init__(self) -> None:
        """Reject a verb the menu could not draw or the key path could not judge."""
        if cell_len(self.key) != 1 or self.key.isspace():
            raise ValueError(f"menu key {self.key!r} must be one printable character")
        if not self.verb.strip() or cell_len(self.verb) > VERB_COLUMN:
            raise ValueError(f"verb {self.verb!r} must be 1 to {VERB_COLUMN} cells")
        if self.available == bool(self.reason):
            state = "an available verb takes no" if self.available else "a refused verb needs a"
            raise ValueError(f"verb {self.verb!r}: {state} reason")
        if self.target is not None and self.weight != VerbWeight.LIGHT:
            raise ValueError(f"heavy verb {self.verb!r} opens a card, not route {self.target!r}")

    @property
    def mutates(self) -> bool:
        """Return whether the verb writes, which its authority class marks."""
        return bool(self.authority)


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a verb can act right now, and the live reason when it cannot."""

    ok: bool
    why: str = ""


class Outcome(StrEnum):
    """What pressing a verb's letter does."""

    OPEN = "open"
    COPY = "copy"
    REFUSED_TOAST = "refused_toast"
    REFUSED = "refused"
    CONSEQUENCE = "consequence"


def menu_order(verbs: Sequence[MenuVerb]) -> tuple[MenuVerb, ...]:
    """Return ``verbs`` light first, each weight in its declared order."""
    light = tuple(v for v in verbs if v.weight == VerbWeight.LIGHT)
    return light + tuple(v for v in verbs if v.weight != VerbWeight.LIGHT)


def availability(verb: MenuVerb, *, mutable: bool, refusal: str) -> Availability:
    """Return whether ``verb`` can act under the current connection state.

    Args:
        verb: The verb pressed or listed.
        mutable: Whether the connection state admits writes.
        refusal: The live reason a write is refused in this state.
    """
    if not verb.available:
        return Availability(False, verb.reason)
    if verb.mutates and not mutable:
        return Availability(False, refusal)
    return Availability(True)


def outcome(verb: MenuVerb, guard: Availability) -> Outcome:
    """Return what pressing ``verb`` does under ``guard``.

    A light verb opens its target or fills the clipboard, and refuses in an error toast
    rather than a card; a heavy verb opens its consequence card, or records a refusal.
    """
    if verb.weight == VerbWeight.LIGHT:
        if not guard.ok:
            return Outcome.REFUSED_TOAST
        return Outcome.OPEN if verb.target else Outcome.COPY
    return Outcome.CONSEQUENCE if guard.ok else Outcome.REFUSED


def _light_doors(registry: RouteRegistry) -> set[tuple[str | None, str | None, str]]:
    """Return every light-verb door as ``(origin, key, target)``."""
    return {
        (door.origin, door.key, spec.id)
        for spec in registry.routes
        for door in spec.doors
        if door.kind == DoorKind.LIGHT_VERB
    }


def _validate(menus: Mapping[str, tuple[MenuVerb, ...]], registry: RouteRegistry) -> None:
    """Refuse menus the registry does not back.

    Raises:
        ValueError: a menu names an unregistered route, repeats a letter, or holds a light
            verb whose target the registry gives no light-verb door with that letter.
    """
    unknown = sorted(set(menus) - set(registry.ids))
    if unknown:
        raise ValueError(f"action menus for unregistered routes: {', '.join(unknown)}")
    doors = _light_doors(registry)
    for route, verbs in menus.items():
        letters = [v.key for v in verbs]
        repeated = sorted({k for k in letters if letters.count(k) > 1})
        if repeated:
            raise ValueError(f"route {route!r} binds {', '.join(repeated)} to two verbs")
        for v in verbs:
            if v.target is not None and (route, v.key, v.target) not in doors:
                raise ValueError(
                    f"route {route!r} verb {v.key} opens {v.target!r} through no registry door"
                )


class ActionMenus:
    """The verbs each route binds, in menu order.

    Args:
        menus: Verbs per route id, in declared order.
        registry: The route rows the menus are checked against.

    Raises:
        ValueError: the menus fail the registry checks.
    """

    def __init__(
        self,
        menus: Mapping[str, Sequence[MenuVerb]],
        *,
        registry: RouteRegistry = REGISTRY,
    ) -> None:
        ordered = {route: menu_order(verbs) for route, verbs in menus.items()}
        _validate(ordered, registry)
        self._menus: Mapping[str, tuple[MenuVerb, ...]] = MappingProxyType(ordered)

    def verbs(self, route: str) -> tuple[MenuVerb, ...]:
        """Return ``route``'s verbs in menu order; empty for a route that binds none."""
        return self._menus.get(route, ())

    def verb(self, route: str, key: str) -> MenuVerb | None:
        """Return the verb ``key`` runs in ``route``'s menu, if it binds one."""
        return next((v for v in self.verbs(route) if v.key == key), None)


def toggle(session: Session, menus: ActionMenus) -> bool:
    """Open or close the action drawer for the session's route.

    Returns:
        ``False`` when the route binds no verb and nothing opened; the caller raises the
        ``NO ACTIONS`` toast. ``True`` when the drawer opened or closed.
    """
    if session.overlay != ACTIONS_OVERLAY and not menus.verbs(session.route):
        return False
    session.overlay = None if session.overlay == ACTIONS_OVERLAY else ACTIONS_OVERLAY
    session.c_target = None
    return True


def menu_rows(
    verbs: Sequence[MenuVerb], *, guard: Callable[[MenuVerb], Availability], w: int
) -> list[str]:
    """Return the drawer rows: the column heads, then one row per verb, each ``w`` cells.

    A refused verb's reason fills the last column and is word-clipped to the room left.

    Args:
        verbs: The route's verbs in menu order.
        guard: The live availability of each verb.
        w: The frame width in cells.

    Raises:
        ValueError: ``w`` is narrower than the columns before the reason.
    """
    if w < _REASON_AT:
        raise ValueError(f"the action menu needs {_REASON_AT} cells, got {w}")
    lines = [_table_row("ACTIONS", "KEY", "VERB", "REASON", w)]
    for v in verbs:
        check = guard(v)
        lines.append(_table_row("", v.key, v.verb, "" if check.ok else check.why, w))
    if not verbs:
        lines.append(pad(EMPTY_ROW, w))
    return lines


def _table_row(first: str, key: str, verb: str, reason: str, w: int) -> str:
    """Return one menu table row with no caret in its gutter."""
    cells = pad(first, TITLE_COLUMN) + pad(key, KEY_COLUMN) + pad(verb, VERB_COLUMN)
    return pad(_LEAD + cells + clip_words(reason, w - _REASON_AT), w)
