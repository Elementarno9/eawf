"""Quit is a guarded double Escape at a quiet scope home (CON-040).

The first Escape arms; a second Escape more than 80 ms and less than 1.5 s later quits; a
second Escape later than that re-arms; any other key disarms. The guard accepts the press
only at scope home with nothing open, no armed prefix, an empty back stack and no
outstanding control. Each outcome is written to the key log, which the ``--verbose`` row
prints. The keys go through the production dispatcher on a held clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import QUIT_CEILING, QUIT_FLOOR, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import BackEntry, Session

HOME = "scope.home"


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> FakeClock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


def _fixture() -> Fixture:
    root = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"
    return load_fixture(root)


FIXTURE = _fixture()


def _home() -> Session:
    session = Session()
    session.route = HOME
    compose_frame(View(session=session, fixture=FIXTURE, w=80, h=24))
    return session


def _press(session: Session, host: _Host, key: str, *, outstanding: int = 0) -> None:
    ctx = Ctx(session=session, fixture=FIXTURE, host=host, w=80, h=24, outstanding=outstanding)
    dispatch(ctx, key, False)
    compose_frame(View(session=session, fixture=FIXTURE, w=80, h=24))


def _twice(gap: float, session: Session | None = None) -> tuple[Session, _Host]:
    session = session or _home()
    host = _Host()
    _press(session, host, "Escape")
    host.clock.advance(gap)
    _press(session, host, "Escape")
    return session, host


@pytest.mark.parametrize(
    ("gap", "quits"),
    [(0.079, 0), (0.081, 1), (1.49, 1), (1.51, 0)],
    ids=["79ms", "81ms", "1.49s", "1.51s"],
)
def test_con040_second_escape_quits_only_inside_the_window(gap: float, quits: int) -> None:
    """CON-040: 79 ms is a burst, 81 ms and 1.49 s quit, 1.51 s re-arms instead."""
    assert pytest.approx(0.080) == QUIT_FLOOR
    assert pytest.approx(1.5) == QUIT_CEILING
    _session, host = _twice(gap)
    assert host.quits == quits


def test_con040_each_outcome_is_written_to_the_key_log() -> None:
    """CON-040: armed, burst, re-armed and quit each leave a line the verbose row prints."""
    session, _host = _twice(0.079)
    assert session.log[1].note.startswith("at scope home · press again")
    assert session.log[0].note == "too fast to be two presses — still armed"
    session, _host = _twice(1.51)
    assert session.log[0].note.startswith("at scope home · press again")
    session, _host = _twice(0.5)
    assert session.log[0].key == "Esc Esc"
    assert session.log[0].note.startswith("quit — guarded: scope home, nothing open")
    frame = compose_frame(View(session=session, fixture=FIXTURE, w=80, h=24, verbose=True))
    assert any("Esc Esc → quit" in row for row in frame)


def test_con040_any_other_key_disarms_and_says_so() -> None:
    """CON-040: a key between the presses disarms, so the next Escape only arms again."""
    session = _home()
    host = _Host()
    _press(session, host, "Escape")
    _press(session, host, "ArrowDown")
    assert session.last_esc == 0.0
    assert any(entry.note.startswith("quit disarmed") for entry in session.log)
    host.clock.advance(0.5)
    _press(session, host, "Escape")
    assert host.quits == 0


def test_con040_escape_with_an_overlay_open_closes_it_and_never_arms() -> None:
    """CON-040: an open overlay takes the Escape; the guard is not armed."""
    session = _home()
    session.overlay = "help"
    host = _Host()
    _press(session, host, "Escape")
    assert session.overlay is None
    assert session.last_esc == 0.0
    assert host.quits == 0


def test_con040_escape_with_the_prefix_armed_cancels_it_and_never_arms() -> None:
    """CON-040: an armed ``g`` takes the Escape as its cancel."""
    session = _home()
    host = _Host()
    _press(session, host, "g")
    _press(session, host, "Escape")
    assert session.prefix is None
    assert session.last_esc == 0.0


def test_con040_a_back_stack_turns_escape_into_back() -> None:
    """CON-040: at scope home with somewhere to go back to, Escape goes back."""
    session = _home()
    session.back.record(BackEntry(route="activity", sel=0, subj=None))
    host = _Host()
    _press(session, host, "Escape")
    host.clock.advance(0.5)
    assert session.route == "activity"
    assert host.quits == 0


def test_con040_an_outstanding_control_holds_the_quit() -> None:
    """CON-040: while the daemon owes an answer, Escape at home neither arms nor quits."""
    session = _home()
    host = _Host()
    _press(session, host, "Escape", outstanding=1)
    host.clock.advance(0.5)
    _press(session, host, "Escape", outstanding=1)
    assert host.quits == 0
    assert session.last_esc == 0.0
    assert "outstanding" in session.log[0].note


def test_con040_escape_off_scope_home_never_quits() -> None:
    """CON-040: on any other route a quick Escape Escape goes back and up, never out."""
    session = Session()
    session.route = "activity"
    compose_frame(View(session=session, fixture=FIXTURE, w=80, h=24))
    session, host = _twice(0.5, session)
    assert host.quits == 0


@pytest.mark.parametrize("key", ["q", "Q"])
def test_con040_there_is_no_q_binding(key: str) -> None:
    """CON-040: ``q`` does not exit, at scope home or anywhere else."""
    session = _home()
    host = _Host()
    _press(session, host, key)
    _press(session, host, key)
    assert host.quits == 0
    assert session.trace == f"{key} → unclaimed"
