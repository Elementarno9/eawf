"""What the overlay and drawer chassis tests share: a host, a link, a session and a key path.

Every helper drives the console the way the app does -- the frame is composed before each
key, because a renderer publishes the counts and navigation the dispatcher reads -- so a
test states the keys it presses rather than the order the app calls things in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import VerbRequest
from eawf.surfaces.tui.console.session import SIZES, Session

FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"


class Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


class Link:
    """A daemon link that records every verb handed to it."""

    def __init__(self) -> None:
        self.sent: list[VerbRequest] = []

    def __call__(self, request: VerbRequest) -> bool:
        self.sent.append(request)
        return True


def prototype() -> Fixture:
    """Return the prototype registers the golden contract replays."""
    return load_fixture(FIXTURE_DIR)


def chrome() -> Fixture:
    """Return the registers a console holding only its packaged chrome draws from."""
    return Fixture.from_chrome(load_chrome())


def session_on(route: str, **fields: Any) -> Session:
    """Return a fresh session on ``route`` with ``fields`` set."""
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    return session


def view_of(session: Session, fixture: Fixture, size: int = 1) -> View:
    """Return the render view of ``session`` at size index ``size``."""
    w, h = SIZES[size]
    return View(session=session, fixture=fixture, w=w, h=h)


def frame_of(session: Session, fixture: Fixture, size: int = 1) -> list[str]:
    """Return the composed frame of ``session`` at size index ``size``."""
    return compose_frame(view_of(session, fixture, size))


def surface_of(session: Session) -> str | None:
    """Return the open overlay or drawer, read afresh after a key moved it."""
    return session.overlay


def press(session: Session, fixture: Fixture, *keys: str, link: Link | None = None) -> None:
    """Draw the frame, then dispatch each key the way the app does."""
    for key in keys:
        compose_frame(view_of(session, fixture))
        ctx = Ctx(session=session, fixture=fixture, host=Host(), w=120, h=30, send=link)
        dispatch(ctx, key, False)
