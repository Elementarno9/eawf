"""What a key handler works with, the focus grammar, and the one way a route opens another.

A :class:`Ctx` is built per keystroke by the app: the session, the registers, the host
that owns the clock and the quit, the frame size, the ``--verbose`` flag and the read
model the frame was drawn from. Route key hooks and the dispatcher both receive it, so a
route's own keys live beside its renderer without the dispatcher knowing them, and a key
that acts on what the frame shows reads the same model the frame drew rather than a
second answer of its own.

The focus grammar lives here too, so the dispatcher and a route's own keys read one
answer. A route's focus regions are the ones its registry row declares, and the Tab
destination is the next of them; the selection is the stable id the session holds; and a
step onto the back stack carries every cursor the place was left with, so Escape restores
the place rather than its first row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from eawf.kernel.projection.compute import ProjectionRow, RouteProjection
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.settings import EffectiveSettingsView
from eawf.kernel.projection.spine import SpineView
from eawf.surfaces.tui.console.clock import Clock, notify
from eawf.surfaces.tui.console.decisions import DecisionRecords
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.operations import VerbRequest
from eawf.surfaces.tui.console.registry import REGISTRY, SURFACES
from eawf.surfaces.tui.console.session import BackEntry, FocusTarget, Session
from eawf.surfaces.tui.console.tokens import Severity

# The key-log key a navigation that no key names directly is recorded under.
NAV_KEY = "—"
#: The depth keys: Enter drills, Escape returns, ``u`` climbs the containment chain, and
#: ``[`` and ``]`` walk the siblings at the current depth. Breadth is the ``g`` prefix.
DEPTH_KEYS: tuple[str, ...] = ("Enter", "Escape", "u", "[", "]")


class Host(Protocol):
    """What the dispatcher needs from the app: the console clock and a quit."""

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        ...

    def quit(self) -> None:
        """End the console session."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Ctx:
    """One keystroke's context.

    Attributes:
        session: The session the key acts on.
        fixture: The registers.
        host: The app that owns the clock and the quit.
        w: The frame width in cells.
        h: The frame height in rows.
        verbose: Whether an unclaimed key is named in the key log.
        projection: The read model the frame was drawn from, when one is held. A key
            that acts on what the frame shows reads this rather than deriving a second
            answer the operator never saw.
        unheld: Whether the frame is the unknown frame: the console holds no prototype
            rows and no read model for the route. A route's own keys act on rows the
            frame drew, so they claim nothing while it drew none.
        send: Hands a confirmed verb to the daemon link, answering whether a link took
            it; the answer arrives later, on the link's own time. ``None`` when the
            console has no daemon link, where a confirmed verb is sent nowhere.
        attention: The Attention projection the link holds. An answer is addressed only
            to a row of it; with none held there is nothing to answer.
        principal_refusal: Why every bound write is refused because the link acts as
            nobody; empty when it acts as someone or there is no link.
        settings: The effective-settings view the settings frame was drawn from; an
            edit is previewed and addressed from it, never from the prototype catalog.
        outstanding: How many sent operations the daemon has not answered yet; the
            guarded quit waits while any is outstanding.
        rows: Every row the link's held projections carry. A lifecycle move is previewed
            from the row's own status and revision, and nowhere else.
        decisions: The records the frame's decision overlay or card was drawn from; a
            key on it acts on the same record.
        principal: Who the console acts as, whose own top attention item ``!`` jumps to;
            ``None`` when it acts as nobody.
        scope: The scope a linked console is attached to, by name else by id, which an
            address names where no held row answers it; empty with no link.
        gutter: The blank cells kept clear at each side of the frame, so a key that lays
            the frame out again steps on the same terminal width the render did.
    """

    session: Session
    fixture: Fixture
    host: Host
    w: int
    h: int
    verbose: bool = False
    projection: SpineView | RouteReadModel | None = None
    unheld: bool = False
    send: Callable[[VerbRequest], bool] | None = None
    attention: RouteProjection | None = None
    principal_refusal: str = ""
    settings: EffectiveSettingsView | None = None
    outstanding: int = 0
    rows: tuple[ProjectionRow, ...] = ()
    decisions: DecisionRecords | None = None
    principal: str | None = None
    scope: str = ""
    gutter: int = 0

    @property
    def s(self) -> Session:
        """Return the session."""
        return self.session

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self.host.clock

    def notify(self, text: str, title: str = "done", sev: Severity = Severity.INFO) -> None:
        """Raise a toast on the rack."""
        notify(self.session, self.clock, text=text, title=title, sev=sev)

    def log(self, key: str, note: str = "") -> None:
        """Record the handler that claimed ``key``."""
        self.session.log_key(key, note)

    def noop(self, key: str) -> None:
        """Record an unclaimed key."""
        self.session.noop(key, verbose=self.verbose)

    def dispatch_write(self, request: VerbRequest) -> bool:
        """Hand ``request`` to the daemon link.

        Args:
            request: The confirmed verb to send.

        Returns:
            Whether a link took the verb; ``False`` when there is no link to take it.
        """
        return self.send is not None and self.send(request)


def regions_of(route: str) -> tuple[str, ...]:
    """Return the focus regions ``route`` declares, in Tab order; none for a one-place frame."""
    return REGISTRY.focus_regions.get(route, ())


def focused_region(session: Session) -> str | None:
    """Return the region holding the arrows: the held one while declared, else the first.

    A region named for another route is not this route's, so it reads as the first.
    """
    regions = regions_of(session.route)
    if not regions:
        return None
    return session.region if session.region in regions else regions[0]


def cycle[T](items: Sequence[T], current: T, *, back: bool) -> T:
    """Return the item after ``current`` in ``items``, or before it on ``back``, wrapping.

    An item not in ``items`` counts as sitting before the first, so the step forward
    lands on the first and the step back on the last.

    Raises:
        ValueError: ``items`` is empty.
    """
    if not items:
        raise ValueError("there is nothing to cycle through")
    at = items.index(current) if current in items else -1 if not back else 0
    return items[(at + (-1 if back else 1)) % len(items)]


def cycle_region(session: Session, *, back: bool = False) -> str | None:
    """Move the focus to the next declared region, or the previous one on ``back``.

    Returns:
        The region now focused; ``None`` on a route with fewer than two regions, where
        there is nowhere for the focus to go and nothing changes.
    """
    regions = regions_of(session.route)
    if len(regions) < 2:
        return None
    at = regions.index(focused_region(session) or regions[0])
    session.region = regions[(at + (-1 if back else 1)) % len(regions)]
    return session.region


def remember(session: Session) -> BackEntry:
    """Return the back-stack step for where the session is now, every cursor included."""
    return BackEntry(
        route=session.route,
        sel=session.sel,
        subj=session.subj_id,
        sel_id=session.sel_id,
        bucket=session.bucket,
        filter=session.filters.get(session.route, ""),
        scroll=session.scroll,
        evt=session.evt,
        region=session.region,
    )


def recall(session: Session, entry: BackEntry) -> None:
    """Put the session back where ``entry`` left it, restoring every cursor it carries."""
    session.route = entry.route
    session.subj_id = entry.subj
    session.sel = entry.sel
    session.sel_id = entry.sel_id
    session.bucket = entry.bucket
    session.filters[entry.route] = entry.filter
    session.scroll = entry.scroll
    session.evt = entry.evt
    session.region = entry.region


def focus_target(session: Session) -> FocusTarget:
    """Return the row and region focus returns to when what opens now closes."""
    return FocusTarget(
        route=session.route, sel=session.sel, sel_id=session.sel_id, region=session.region
    )


def return_focus(session: Session) -> bool:
    """Put focus back on the row and region an overlay was opened from.

    Returns:
        Whether focus moved back; ``False`` when nothing was recorded or the session has
        since left the route the target was on.
    """
    target = session.focus_return
    session.focus_return = None
    if target is None or target.route != session.route:
        return False
    session.sel = target.sel
    session.sel_id = target.sel_id
    session.region = target.region
    return True


def busy(session: Session) -> bool:
    """Return whether an overlay, the filter, the go prefix or an editor owns the keys."""
    return bool(session.overlay or session.typing or session.prefix or session.edit)


def has_renderer(route: str) -> bool:
    """Return whether ``route`` is a registered route the console can open."""
    return route in REGISTRY.by_id


def go(ctx: Ctx, route: str, why: str, entity_id: str | None = None) -> bool:
    """Open ``route`` onto ``entity_id``, pushing the current place on the back stack.

    Args:
        ctx: The keystroke's context.
        route: The route to open.
        why: What opened it, as the key log records it.
        entity_id: The subject to open the route onto, if any.

    Returns:
        Whether the console moved; ``False`` for an unregistered route or the current place.
    """
    s = ctx.s
    if not has_renderer(route):
        ctx.log(NAV_KEY, f"{why} → {route} is designed but not bound here")
        return False
    if s.route == route and REGISTRY.subj_now(route, s.subj_id) == REGISTRY.subj_now(
        route, entity_id
    ):
        ctx.log(NAV_KEY, f"{why} · already here")
        return False
    s.back.record(remember(s))
    s.route = route
    s.sel = 0
    s.sel_id = None
    s.region = None
    s.subj_id = entity_id or None
    leave_overlay(s)
    ctx.log(NAV_KEY, f"{why} → {route}")
    return True


def open_overlay(session: Session, name: str, *, subject: str | None = None) -> None:
    """Open overlay or drawer ``name`` above the route, capturing ``subject``.

    An overlay is never a route: the route, its subject and the back stack stay as they
    are. Opening one from the route records where focus stood as the session's one focus
    return; replacing the top overlay with another keeps it, so the chain still returns to
    where it began.

    Args:
        session: The session the overlay opens in.
        name: The overlay or drawer to open.
        subject: What the overlay is about, captured now so nothing inside it can move
            it; ``None`` leaves it to the route's subject.

    Raises:
        KeyError: ``name`` is not an overlay or drawer, so opening it would draw a surface
            nothing registers.
    """
    if name not in SURFACES:
        raise KeyError(f"no overlay or drawer is named {name!r}")
    if session.overlay is None:
        session.focus_return = focus_target(session)
    session.overlay = name
    session.ov_subject = subject


def leave_overlay(session: Session) -> str | None:
    """Drop the open overlay and what it captured because a verb or a navigation acted.

    Where focus goes afterwards is :func:`return_focus`'s: the invoking row the overlay
    recorded stays recorded, and a navigation that leaves the route leaves it unused.

    Returns:
        The overlay that was open, if any.
    """
    left = session.overlay
    session.overlay = None
    session.ov_subject = None
    session.resolution_ending = None
    return left


def close_overlay(session: Session) -> str | None:
    """Dismiss the open overlay and give focus back to the row and region that opened it.

    Dismissing is never a resolution: it writes nothing and only restores the focus.

    Returns:
        The overlay that was open, if any.
    """
    left = leave_overlay(session)
    return_focus(session)
    return left
