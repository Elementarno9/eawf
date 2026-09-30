"""The overlay chassis: an overlay's crumb, its cursor foot and its held frame.

Every overlay names itself and its subject in one crumb form. A cursor overlay names the
row its cursor is on in its foot. Opening and closing an overlay is navigation's
(:mod:`~eawf.surfaces.tui.console.navigation`).

An overlay whose rows nothing has read still holds the frame: it draws its own crumb, a
body stating that its subject is not held, and a keybar of the keys it still binds, so the
route's unknown frame never shows through it. That is ``Esc back`` alone, except on the
consequence card, whose confirm answers the row the daemon link holds rather than a row
it draws.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, bar, build, header, thin
from eawf.surfaces.tui.console.keybar import KEY, Pair, keybar
from eawf.surfaces.tui.console.onboarding import ONBOARDING_KIND
from eawf.surfaces.tui.console.registry import SURFACES
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import BRAND, CRUMB_SEP, TRUTH
from eawf.surfaces.tui.console.width import pad

UNKNOWN = TRUTH["unknown"].unicode
# The overlays drawn from the console's own chrome, which hold on any console.
CHROME_OVERLAYS: frozenset[str] = frozenset({"help", "palette"})
_FOOT_LABEL = 9
_BACK: tuple[Pair, ...] = (KEY["esc"].pair(),)
# The keys an overlay holding nothing still binds, where they are more than Escape.
_UNHELD_PAIRS: Mapping[str, tuple[Pair, ...]] = MappingProxyType(
    {"consequence": (("Enter", "confirm"), ("Esc", "cancel"))}
)
_UNHELD_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType({"consequence": ("Enter", "Escape")})


def unheld_keys(name: str) -> tuple[str, ...]:
    """Return the keys overlay ``name`` binds while it holds nothing it draws."""
    return _UNHELD_KEYS.get(name, ("Escape",))


def crumb(name: str, subject: str) -> str:
    """Return overlay ``name``'s crumb over ``subject``, in the form every overlay shares.

    Raises:
        KeyError: ``name`` is not an overlay or drawer.
    """
    return f" {BRAND}{CRUMB_SEP}{SURFACES[name].title} · {subject}"


def subject_of(session: Session) -> str:
    """Return the open overlay's subject: the one it captured, else the route's, else unknown."""
    return session.ov_subject or session.subj_id or UNKNOWN


def holds(name: str, session: Session, fixture: Fixture) -> bool:
    """Return whether overlay ``name`` has what it draws.

    The chrome overlays always do. A resolution card holds once the ending of its target
    has been captured, because every other word it prints is chrome. A consequence card
    holds once a card was built from what the daemon link holds, or once a first run's
    step was previewed on it. Every other overlay draws from the prototype registers
    until it is drawn from the projection.
    """
    if name in CHROME_OVERLAYS:
        return True
    if name == "resolution" and session.resolution_ending and session.ov_subject:
        return True
    if name == "consequence" and session.mutation is not None:
        return True
    if name == "consequence" and (session.c_target or {}).get("kind") == ONBOARDING_KIND:
        return True
    return fixture.prototype


def cursor_foot(label: str, n: int, total: int) -> str:
    """Return a cursor overlay's foot row: the row the cursor is on, of how many.

    Args:
        label: What the rows are, in the label gutter.
        n: The cursor's row, counted from one.
        total: The rows the cursor walks.
    """
    return f" {pad(label, _FOOT_LABEL)} {n} of {total}"


def unheld_frame(view: View, name: str) -> list[str]:
    """Return overlay ``name``'s frame when nothing it draws has been read.

    Raises:
        KeyError: ``name`` is not an overlay.
    """
    w = view.w
    subject = subject_of(view.session)
    rows = [
        header(view, crumb(name, subject)),
        f" {subject} is not held · nothing has been read for this {SURFACES[name].title}",
        bar(w),
        f" ROWS      {UNKNOWN} unknown · nothing has been read, so no row is drawn",
        thin(w),
    ]
    return build(view, rows, keybar(_UNHELD_PAIRS.get(name, _BACK), w))
