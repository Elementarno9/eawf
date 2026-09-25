"""What a key handler works with, and the one way a route opens another.

A :class:`Ctx` is built per keystroke by the app: the session, the registers, the host
that owns the clock and the quit, the frame size, the ``--verbose`` flag and the read
model the frame was drawn from. Route key hooks and the dispatcher both receive it, so a
route's own keys live beside its renderer without the dispatcher knowing them, and a key
that acts on what the frame shows reads the same model the frame drew rather than a
second answer of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.spine import SpineView
from eawf.surfaces.tui.console.clock import Clock, notify
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import Severity

# The key-log key a navigation that no key names directly is recorded under.
NAV_KEY = "—"


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
    """

    session: Session
    fixture: Fixture
    host: Host
    w: int
    h: int
    verbose: bool = False
    projection: SpineView | RouteReadModel | None = None
    unheld: bool = False

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
    s.back.push(route=s.route, sel=s.sel, subj=s.subj_id)
    s.route = route
    s.sel = 0
    s.sel_id = None
    s.subj_id = entity_id or None
    s.overlay = None
    ctx.log(NAV_KEY, f"{why} → {route}")
    return True
