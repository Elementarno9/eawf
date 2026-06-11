"""Golden snapshots for the agent-watch cosmic-terminal reskin (P30-I02-W17).

Pins the reskin behaviours the wave delivers, each captured from the
:class:`~eawf.surfaces.tui.modes.agent_watch.AgentWatchModeScreen` mounted IN
ISOLATION on a bare themed host (mirroring the research-board reskin suite) so
the frame is a pure function of the bound fixture state with no off-disk daemon
read:

* the running single-session look -- the watched-session header LEADS with the
  RUNNING lifecycle sigil (the filled diamond, not the raw status word alone)
  for an ACTIVE executor session, and the idle cancel line wears the failed-look
  cancel mark (the multiplication-x); and
* the honest-empty sentinel -- a scope with no dispatched executor session
  renders the literal :data:`~eawf.surfaces.tui.modes.agent_watch.EMPTY_NOTICE`
  ("no active dispatched session") rather than implying a live stream.

The multi-session grid is deferred to I06 -- this wave reskins the single
watched session only.

The ``affordance_parity`` half confirms the cancel key resolves to a LIVE
:class:`~textual.binding.Binding` (``k`` -> ``cancel_session`` in the active
binding map) and FIRES: pressing ``k`` moves the result line off its idle copy
to the honest "daemon unavailable" line (the bare host exposes no daemon
socket, so the action surfaces the honest result rather than a faked kill).

Both frames pin the unicode render mode so the sigil column is deterministic.
The host carries only the read-only ``state`` / ``_state_path`` / ``render_mode``
the screen reads; there is no daemon socket, so the cancel action surfaces the
honest no-daemon result.

Regenerate the goldens after an intentional layout change with::

    EAWF_DAEMONLESS=1 EAWF_SNAPSHOT_REGEN=1 uv run pytest \
        tests/snapshots/tui/test_agent_watch_reskin.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    Project,
    State,
)
from eawf.surfaces.tui.modes.agent_watch import (
    CANCEL_IDLE,
    CANCEL_NO_DAEMON,
    EMPTY_NOTICE,
    WATCH_HEADER_ID,
    WATCH_RESULT_ID,
    AgentWatchModeScreen,
    cancel_mark,
    session_sigil_markup,
)
from eawf.surfaces.tui.snapshot import assert_screen_snapshot, settle_screen
from eawf.surfaces.tui.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.widgets.eu_bar import RenderMode
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, tint

_THEME = Path(__file__).resolve().parents[3] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_GOLDEN = Path(__file__).resolve().parent / "golden"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: A wide terminal so the header + stream + result rows lay out unwrapped.
_SIZE = (120, 40)

#: The wave the seeded executor session scopes to (its ``scope_id``).
_WAVE = "P01-I01-W01"

#: The cancel key + its bound action -- the affordance the criterion pins.
_CANCEL_KEY = "k"
_CANCEL_ACTION = "cancel_session"

assert _THEME.is_file(), f"missing theme: {_THEME}"


class _HostApp(App[None]):
    """Bare themed host carrying the read-only surface the screen reads.

    The screen reads ``state`` (the agent sessions) and ``render_mode`` (the
    sigil column) off ``self.app``. The host exposes exactly those and no
    daemon socket, so the cancel action surfaces the honest no-daemon result
    with no app-level brand header / sibling chrome and no off-disk daemon read.
    """

    CSS_PATH = str(_THEME)
    render_mode: reactive[RenderMode] = reactive[RenderMode]("unicode")
    state: reactive[State | None] = reactive(None)

    def __init__(self, *, state: State | None, state_path: Path | None) -> None:
        super().__init__()
        for theme in EA_THEMES:
            self.register_theme(theme)
        self.theme = LOGICAL_THEMES["dark"]
        self.state = state
        self._state_path = state_path

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(AgentWatchModeScreen())

    def _daemon_socket_available(self) -> bool:
        """No daemon under the bare host -- the cancel action surfaces honestly."""
        return False


def _session(
    sid: str = "S-1",
    *,
    scope_id: str = _WAVE,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
) -> AgentSession:
    """Build an executor agent-session row for the watch-target picker."""
    return AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=status,
        started_at=_T0,
    )


def _state(*, sessions: dict[str, AgentSession] | None = None) -> State:
    """Build a minimal repo state, optionally with agent sessions."""
    return State.model_validate(
        {
            "schema_version": "1.3",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": (
                {sid: s.model_dump(mode="json") for sid, s in sessions.items()}
                if sessions is not None
                else {}
            ),
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# Reskin helpers -- the running sigil + the failed-look cancel mark
# --------------------------------------------------------------------------


def test_session_sigil_active_is_the_running_diamond() -> None:
    """An ACTIVE session's header sigil is the tinted RUNNING lifecycle mark."""
    markup = session_sigil_markup(AgentSessionStatus.ACTIVE, mode="unicode")
    assert glyph(Sigil.RUNNING, mode="unicode") in markup
    # The raw status word never leaks into the rendered sigil markup.
    assert AgentSessionStatus.ACTIVE.value not in markup


def test_session_sigil_maps_every_status_to_a_lifecycle_shape() -> None:
    """Every session status renders a lifecycle sigil glyph, never its raw word."""
    for status in AgentSessionStatus:
        markup = session_sigil_markup(status, mode="unicode")
        assert status.value not in markup
    assert glyph(Sigil.CLOSED, mode="unicode") in session_sigil_markup(
        AgentSessionStatus.CLOSED, mode="unicode"
    )
    assert glyph(Sigil.FAILED, mode="unicode") in session_sigil_markup(
        AgentSessionStatus.FAILED, mode="unicode"
    )


def test_cancel_mark_is_the_tinted_failed_x() -> None:
    """The cancel affordance wears the FAILED sigil (the failed-x look)."""
    markup = cancel_mark(mode="unicode")
    assert glyph(Sigil.FAILED, mode="unicode") in markup
    assert tint(Sigil.FAILED) is not None
    assert f"[{tint(Sigil.FAILED)}]" in markup


def test_cancel_mark_ascii_column_is_the_letter_x() -> None:
    """In ASCII mode the cancel mark falls back to the ``x`` glyph column."""
    markup = cancel_mark(mode="ascii")
    assert glyph(Sigil.FAILED, mode="ascii") in markup


# --------------------------------------------------------------------------
# Snapshot: the running single-session look (running sigil + cancel look)
# --------------------------------------------------------------------------


def test_agent_watch_reskin_running_snapshot(tmp_path: Path) -> None:
    """The mounted pane leads the header with the running sigil + cancel look."""
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            assert screen.target is not None
            header = str(screen.query_one(f"#{WATCH_HEADER_ID}", Static).render())
            result = str(screen.query_one(f"#{WATCH_RESULT_ID}", Static).render())
            # The header leads with the RUNNING diamond for an ACTIVE session.
            assert glyph(Sigil.RUNNING, mode=app.render_mode) in header
            # The idle cancel line wears the failed-x cancel mark.
            assert glyph(Sigil.FAILED, mode=app.render_mode) in result
            assert CANCEL_IDLE in result
            assert_screen_snapshot(app, _GOLDEN / "agent_watch_reskin_running.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Snapshot: the honest-empty sentinel (no dispatched executor session)
# --------------------------------------------------------------------------


def test_agent_watch_reskin_empty_snapshot(tmp_path: Path) -> None:
    """The empty pane renders the honest no-active-session sentinel."""
    state = _state()
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            assert screen.target is None
            header = str(screen.query_one(f"#{WATCH_HEADER_ID}", Static).render())
            # The honest-empty sentinel renders rather than implying a stream.
            assert EMPTY_NOTICE in header
            assert_screen_snapshot(app, _GOLDEN / "agent_watch_reskin_empty.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# affordance_parity: cancel key resolves to a live Binding and fires
# --------------------------------------------------------------------------


def test_agent_watch_cancel_key_resolves_to_live_binding(tmp_path: Path) -> None:
    """The cancel key resolves to a LIVE ``Binding`` in the active map.

    The ``affordance_parity`` resolution half: ``k`` is present in the mounted
    screen's active binding map and resolves to a real
    :class:`~textual.binding.Binding` whose action is ``cancel_session`` -- not
    a dead key the footer advertises but no binding answers.
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> Binding | None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            entry = app.screen.active_bindings.get(_CANCEL_KEY)
            return entry.binding if entry is not None else None

    binding = asyncio.run(body())
    assert binding is not None
    assert isinstance(binding, Binding)
    assert binding.action == _CANCEL_ACTION


def test_agent_watch_cancel_key_fires_and_moves_the_result(tmp_path: Path) -> None:
    """Pressing the cancel key FIRES the action and moves the result off idle.

    The ``affordance_parity`` firing half: a real ``k`` keypress (the genuine
    key->Binding path, not a direct action call) opens the FA4 confirm gate;
    confirming ``Yes`` drives ``action_cancel_session`` and the result line
    moves off its idle cancel-look copy to the honest "daemon unavailable"
    line (the bare host exposes no daemon socket, so the cancel surfaces the
    honest result rather than a faked kill).
    """
    state = _state(sessions={"S-1": _session("S-1")})
    state_path = _write_state(tmp_path, state)

    async def body() -> str:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            await pilot.press(_CANCEL_KEY)  # the real key->Binding path
            await settle_screen(pilot)
            # FA4: k opens a confirm gate; confirm Yes to fire the cancel.
            await pilot.press("right")  # highlight Yes
            await pilot.press("enter")  # confirm
            await settle_screen(pilot)
            return str(screen.query_one(f"#{WATCH_RESULT_ID}", Static).render())

    rendered = asyncio.run(body())
    # The press fired: the result moved off the idle line to the honest result.
    assert CANCEL_NO_DAEMON in rendered
    assert CANCEL_IDLE not in rendered


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-braces: never resolve a real daemon socket under the host."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
