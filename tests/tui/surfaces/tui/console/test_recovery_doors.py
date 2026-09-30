"""UI-047: from a lost projection, each Recovery door is taken by Enter and leaves its value.

The console is driven by keys through the real :class:`ConsoleApp` over a seam that reads
an epoch-2 document the way a daemon answers it. The projection is lost (the link drops
to ``DISCONNECTED``), the operator opens the Recovery route, walks to a door and presses
Enter; the connection value the header then carries is the one the door states, and the
door went through the daemon's own read or reconnect verb rather than a fourth path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from eawf.kernel.projection.connection import (
    READ_METHOD_TEMPLATE,
    RECONNECT_METHOD_TEMPLATE,
    ConnectionValue,
    ReconnectDisposition,
)
from eawf.surfaces.tui.console.harness import settle
from eawf.surfaces.tui.console.renderers.crash_recovery import NO_LINK
from eawf.surfaces.tui.console.session import Session, conn_label
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies

ROUTE = "crash.recovery"


def _replay_answer() -> dict[str, Any]:
    return {
        "negotiation": {
            "schema_version": "1.0",
            "route": ROUTE,
            "disposition": ReconnectDisposition.REPLAY.value,
            "client_cursor": 41190,
            "server_cursor": bodies.CURSOR,
            "gap": {"first_sequence": 41191, "last_sequence": bodies.CURSOR},
            "retention": {"first_sequence": 1, "last_sequence": bodies.CURSOR},
        },
        "patches": [],
    }


class _Counting(js.DocumentDaemon):
    """The document daemon, counting each verb a door calls."""

    def __init__(self) -> None:
        super().__init__(bodies.DOCUMENT)
        self.calls: list[str] = []
        self.reconnect_answer = _replay_answer()

    def answer(self, method: str, params: Any) -> dict[str, Any]:
        self.calls.append(method)
        return super().answer(method, params)


def _take(door_index: int) -> tuple[str, str, list[str]]:
    """Lose the projection, open Recovery, walk to door ``door_index`` and press Enter.

    Returns:
        The header connection label, the frame text, and the verbs the door called.
    """
    daemon = _Counting()

    async def body() -> tuple[str, str, list[str]]:
        app = js.held_app(daemon, route=ROUTE)
        async with js.driven(app) as harness:
            seam = app.seam
            assert seam is not None
            await js.walk(harness, {"route": ROUTE, "size": 1}, [])
            # the projection is lost: the link drops and nothing arrives
            await seam._on_degraded(True)
            assert seam.connection is ConnectionValue.DISCONNECTED
            before = len(daemon.calls)
            await js.walk(
                harness, {"route": ROUTE, "size": 1}, ["ArrowDown"] * door_index + ["Enter"]
            )
            await app.workers.wait_for_complete()
            app.render_frame()
            frame, _cycles = await settle(harness.pilot)
            return app.session.conn, frame, daemon.calls[before:]

    return asyncio.run(body())


@pytest.mark.parametrize(
    ("index", "value", "verb"),
    [
        (0, ConnectionValue.GAP, READ_METHOD_TEMPLATE.format(route=ROUTE)),
        # the replay closes its exact gap inside the door, so it lands live
        (1, ConnectionValue.LIVE_COMPLETE, RECONNECT_METHOD_TEMPLATE.format(route=ROUTE)),
        (2, ConnectionValue.OFFLINE_SNAPSHOT, None),
    ],
)
def test_ui_047_enter_takes_the_marked_door_and_leaves_its_connection_value(
    index: int, value: ConnectionValue, verb: str | None
) -> None:
    conn, frame, calls = _take(index)
    assert conn == conn_label(value)
    assert frame.split("\n")[0].rstrip().endswith(conn_label(value))
    if verb is None:
        # read-only reads nothing: it attaches to the snapshot the console holds
        assert not [call for call in calls if call.startswith("projection.crash")]
    else:
        assert verb in calls


def test_ui_047_a_console_with_no_link_takes_no_door_and_says_so() -> None:
    """With no daemon link the door keys refuse rather than pretend a door was taken."""
    from eawf.surfaces.tui.console.dispatch import dispatch
    from eawf.surfaces.tui.console.navigation import Ctx
    from tests.tui.surfaces.tui.console.test_focus_regions_and_selection import _fixture, _Host

    session = Session()
    session.route = ROUTE
    ctx = Ctx(session=session, fixture=_fixture(), host=_Host(), w=120, h=30)
    dispatch(ctx, "Enter", False)
    assert session.log[-1].note == NO_LINK
