"""The watch pane labels an externally-dispatched session honestly.

A session the daemon never spawned emits its events inside another harness.
Nothing reaches this pane unless that harness forwards its output, so the
live-waiting wording ("waiting for session events...") promises a stream that
never arrives. These tests pin the four empty-notice states and the record that
separates them:

* :func:`~eawf.surfaces.tui.modes.agent_watch.has_daemon_dispatch_record` --
  the daemon stamps a ``SessionAttempt`` row and a ``DispatchAnnotation`` per
  spawn it drives, and an outside harness stamps neither;
* :func:`~eawf.surfaces.tui.modes.agent_watch.watch_empty_notice` -- the pure
  notice resolver, including the precedence that keeps the nothing-watched and
  daemon-unreachable wording unchanged and keeps the waiting wording once
  output has been forwarded; and
* the mounted pane, where the notice is what the operator actually reads.

The footer half re-pins the affordance-parity contract: relabelling the empty
notice must leave every advertised key bound to a live action.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    DispatchNote,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    DispatchAnnotation,
    Project,
    SessionAttempt,
    State,
    Wave,
)
from eawf.surfaces.tui.chassis.theme import EA_THEMES, LOGICAL_THEMES
from eawf.surfaces.tui.modes.agent_watch import (
    EMPTY_NOTICE,
    WATCH_DEGRADED,
    WATCH_EMPTY_ID,
    WATCH_EXTERNAL_DISPATCH,
    AgentWatchModeScreen,
    WatchTarget,
    has_daemon_dispatch_record,
    watch_empty_notice,
)
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets.eu_bar import RenderMode

_T0 = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_WAVE = "P01-I01-W01"
_SIZE = (120, 40)
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"
_WAITING = "waiting for session events..."


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _session(
    sid: str = "S-1",
    *,
    scope_id: str = _WAVE,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
) -> AgentSession:
    """Build an executor agent-session row for the watch-target picker."""
    return AgentSession(
        id=sid,
        role=role,
        runtime="claude",
        scope_id=scope_id,
        status=status,
        started_at=_T0,
    )


def _attempt(attempt: int = 1) -> SessionAttempt:
    """Build the session-attempt row the daemon stamps for a spawn it drove."""
    return SessionAttempt(
        attempt=attempt,
        runtime="claude",
        session_id="vendor-digest",
        session_log_handle=f"urn:eawf:v1:session-log:claude:{attempt}",
        started_at=_T0,
    )


def _annotation(attempt: int = 1) -> DispatchAnnotation:
    """Build the dispatch annotation the daemon appends beside an attempt row."""
    return DispatchAnnotation(
        attempt=attempt,
        note=DispatchNote.FRESH_DISPATCH,
        runtime_to="claude",
        occurred_at=_T0,
    )


def _wave(
    *,
    sessions: dict[int, SessionAttempt] | None = None,
    dispatch_history: list[DispatchAnnotation] | None = None,
) -> Wave:
    """Build the watched wave, optionally carrying daemon dispatch rows."""
    return Wave(
        id=_WAVE,
        iter_id="P01-I01",
        title=f"Wave {_WAVE}",
        status=WaveStatus.IN_PROGRESS,
        opened_at=_T0,
        sessions=sessions or {},
        dispatch_history=dispatch_history or [],
    )


def _state(
    *,
    sessions: dict[str, AgentSession] | None = None,
    waves: dict[str, Wave] | None = None,
) -> State:
    """Build a minimal repo state carrying the sessions + waves under test."""
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
            "waves": (
                {wid: w.model_dump(mode="json") for wid, w in waves.items()}
                if waves is not None
                else {}
            ),
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


def _target(*, attempt: int = 1) -> WatchTarget:
    """Build the watched target for the wave under test."""
    return WatchTarget(
        session_id="S-1",
        wave_id=_WAVE,
        runtime="claude",
        status=AgentSessionStatus.ACTIVE,
        attempt=attempt,
    )


class _HostApp(App[None]):
    """Bare themed host exposing only the read-only surface the screen reads."""

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
        """No daemon under the bare host, so no action fakes a live one."""
        return False


def _mounted_notice(tmp_path: Path, state: State) -> str:
    """Mount the watch pane over *state* and return its empty-notice text."""
    state_path = _write_state(tmp_path, state)
    captured: list[str] = []

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            assert screen.target is not None
            captured.append(str(screen.query_one(f"#{WATCH_EMPTY_ID}", Static).render()))

    asyncio.run(body())
    return captured[0]


# --------------------------------------------------------------------------
# has_daemon_dispatch_record -- what counts as a daemon dispatch
# --------------------------------------------------------------------------


def test_has_daemon_dispatch_record_unbound_state_is_false() -> None:
    """An unbound state records no dispatch (the empty boundary)."""
    assert has_daemon_dispatch_record(None, wave_id=_WAVE, attempt=1) is False


def test_has_daemon_dispatch_record_unknown_wave_is_false() -> None:
    """A wave absent from state records no dispatch."""
    assert has_daemon_dispatch_record(_state(), wave_id=_WAVE, attempt=1) is False


def test_has_daemon_dispatch_record_wave_without_rows_is_false() -> None:
    """A known wave with neither an attempt row nor an annotation is external."""
    state = _state(waves={_WAVE: _wave()})

    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=1) is False


def test_has_daemon_dispatch_record_session_attempt_row_is_true() -> None:
    """The wave-local session-attempt row is a daemon dispatch record."""
    state = _state(waves={_WAVE: _wave(sessions={1: _attempt(1)})})

    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=1) is True


def test_has_daemon_dispatch_record_dispatch_annotation_is_true() -> None:
    """A dispatch annotation alone is a daemon dispatch record."""
    state = _state(waves={_WAVE: _wave(dispatch_history=[_annotation(1)])})

    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=1) is True


def test_has_daemon_dispatch_record_other_attempt_is_false() -> None:
    """A record for attempt 1 does not cover attempt 2 (the off-by-one edge)."""
    state = _state(
        waves={_WAVE: _wave(sessions={1: _attempt(1)}, dispatch_history=[_annotation(1)])}
    )

    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=2) is False


def test_has_daemon_dispatch_record_reads_the_matching_attempt() -> None:
    """A wave with several attempts answers per attempt, not per wave."""
    state = _state(waves={_WAVE: _wave(sessions={1: _attempt(1), 3: _attempt(3)})})

    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=3) is True
    assert has_daemon_dispatch_record(state, wave_id=_WAVE, attempt=2) is False


# --------------------------------------------------------------------------
# watch_empty_notice -- the four honest states, in precedence order
# --------------------------------------------------------------------------


def test_watch_empty_notice_no_target_is_the_nothing_watched_line() -> None:
    """With nothing watched the notice is the unchanged honest-empty line."""
    notice = watch_empty_notice(None, degraded=False, state=None, forwarded_chunks=False)

    assert notice == EMPTY_NOTICE


def test_watch_empty_notice_degraded_wording_is_unchanged() -> None:
    """A degraded App keeps the daemon-unreachable wording, external or not."""
    notice = watch_empty_notice(
        _target(), degraded=True, state=_state(waves={_WAVE: _wave()}), forwarded_chunks=False
    )

    assert notice == WATCH_DEGRADED
    assert WATCH_EXTERNAL_DISPATCH not in notice


def test_watch_empty_notice_without_a_dispatch_record_is_external() -> None:
    """No dispatch record and no forwarded chunk reads as externally dispatched."""
    notice = watch_empty_notice(
        _target(), degraded=False, state=_state(waves={_WAVE: _wave()}), forwarded_chunks=False
    )

    assert WATCH_EXTERNAL_DISPATCH in notice
    assert _WAITING not in notice
    # The label still names WHICH session the claim is about.
    assert _target().label in notice


def test_watch_empty_notice_with_a_session_attempt_row_keeps_waiting() -> None:
    """A daemon-dispatched session keeps the live-waiting wording."""
    state = _state(waves={_WAVE: _wave(sessions={1: _attempt(1)})})

    notice = watch_empty_notice(_target(), degraded=False, state=state, forwarded_chunks=False)

    assert notice.endswith(_WAITING)
    assert WATCH_EXTERNAL_DISPATCH not in notice


def test_watch_empty_notice_with_a_dispatch_annotation_keeps_waiting() -> None:
    """An annotation-only record (the attempt row not yet stamped) still waits."""
    state = _state(waves={_WAVE: _wave(dispatch_history=[_annotation(1)])})

    notice = watch_empty_notice(_target(), degraded=False, state=state, forwarded_chunks=False)

    assert notice.endswith(_WAITING)


def test_watch_empty_notice_with_forwarded_chunks_keeps_waiting() -> None:
    """Forwarded output outranks the external label: the stream IS arriving."""
    state = _state(waves={_WAVE: _wave()})

    notice = watch_empty_notice(_target(), degraded=False, state=state, forwarded_chunks=True)

    assert notice.endswith(_WAITING)
    assert WATCH_EXTERNAL_DISPATCH not in notice


def test_watch_empty_notice_unbound_state_with_a_target_is_external() -> None:
    """A target with no state to read records nothing, so it reads external."""
    notice = watch_empty_notice(_target(), degraded=False, state=None, forwarded_chunks=False)

    assert WATCH_EXTERNAL_DISPATCH in notice


# --------------------------------------------------------------------------
# The mounted pane -- what the operator reads
# --------------------------------------------------------------------------


def test_watch_pane_labels_an_undispatched_session_as_external(tmp_path: Path) -> None:
    """The mounted pane renders the external notice for a session state owns alone."""
    state = _state(sessions={"S-1": _session()}, waves={_WAVE: _wave()})

    notice = _mounted_notice(tmp_path, state)

    assert WATCH_EXTERNAL_DISPATCH in notice
    assert _WAITING not in notice


def test_watch_pane_keeps_waiting_for_a_daemon_dispatched_session(tmp_path: Path) -> None:
    """The mounted pane keeps the waiting wording once the daemon stamped a row."""
    state = _state(
        sessions={"S-1": _session()},
        waves={_WAVE: _wave(sessions={1: _attempt(1)}, dispatch_history=[_annotation(1)])},
    )

    notice = _mounted_notice(tmp_path, state)

    assert _WAITING in notice
    assert WATCH_EXTERNAL_DISPATCH not in notice


def test_watch_pane_nothing_watched_notice_is_unchanged(tmp_path: Path) -> None:
    """A scope with no dispatched session keeps the honest-empty banner."""
    state = _state()
    state_path = _write_state(tmp_path, state)
    captured: list[str] = []

    async def body() -> None:
        app = _HostApp(state=state, state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            assert screen.target is None
            captured.append(str(screen.query_one(f"#{WATCH_EMPTY_ID}", Static).render()))

    asyncio.run(body())

    assert EMPTY_NOTICE in captured[0]
    assert WATCH_EXTERNAL_DISPATCH not in captured[0]


# --------------------------------------------------------------------------
# Footer parity -- the relabel leaves every advertised key bound
# --------------------------------------------------------------------------


def test_agent_watch_footer_keys_stay_bound_to_live_actions() -> None:
    """Every key the zoom footer advertises resolves to a live screen action.

    The strip advertises ``x`` (cancel), ``space`` (pause new dispatches) and
    ``Esc`` (back) as screen-owned keys; the remaining tokens (scope, palette,
    help, quit) are App-level. Each screen-owned token must map to a concrete
    :class:`~textual.binding.Binding` whose ``action_*`` method exists.
    """
    advertised = {"x": "x", "space": "space", "Esc": "escape"}
    hints = " ".join(AgentWatchModeScreen.FOOTER_HINTS)
    keys = {
        binding.key: binding.action
        for binding in AgentWatchModeScreen.BINDINGS
        if isinstance(binding, Binding)
    }

    for token, key in advertised.items():
        assert token in hints
        action = keys.get(key)
        assert action is not None, f"advertised key is unbound: {token}"
        assert callable(getattr(AgentWatchModeScreen, f"action_{action}"))


def test_agent_watch_bindings_have_no_dead_action() -> None:
    """Every live binding on the zoom (advertised or not) has an action method."""
    for binding in AgentWatchModeScreen.BINDINGS:
        assert isinstance(binding, Binding)
        assert callable(getattr(AgentWatchModeScreen, f"action_{binding.action}"))
