"""Live-behaviour probes for the new fleet / verdict panes (P30-I07-W12).

This module audits the W05/W09/W17 Watch-mode panes against the project's four
TUI lessons and pins the live behaviour the snapshot + Pilot tests cannot:

* **push beside poll** -- a fresh daemon state revision pushed into a mounted
  fleet pane re-renders the rows WITHOUT an app restart
  (:func:`~eawf.surfaces.tui.snapshot.pilot_harness.push_state_revision`);
* **always-on poll backstop** -- with the daemon push leg dark (no socket), the
  binder's mtime-gated poll loop still refreshes the bound state when the
  on-disk ``state.json`` advances
  (:func:`~eawf.surfaces.tui.snapshot.pilot_harness.tick_poll_backstop`); and
* **non-no-op app.-namespaced actions** -- the new mutating cancel key
  (``k`` -> ``cancel_session``) resolves to a real ``action_*`` handler on the
  screen namespace rather than a silent no-op, and Textual's own
  ``run_action`` dispatcher fires it
  (:func:`~eawf.surfaces.tui.snapshot.pilot_harness.mutating_action_keys_resolve`).

The three new harness probes themselves are unit-checked here too (boundary +
error paths) so the reusable live-behaviour API is covered, not only its first
caller. Determinism follows the project Pilot-worker rule: each Pilot body
drains workers via
:func:`~eawf.surfaces.tui.snapshot.pilot_harness.settle_screen` before
asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.agent_watch import (
    AgentWatchModeScreen,
    WatchGrid,
)
from eawf.surfaces.tui.snapshot.pilot_harness import (
    mutating_action_keys_resolve,
    push_state_revision,
    settle_screen,
    tick_poll_backstop,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The digit key that switches to the Watch mode.
_WATCH_DIGIT = "8"

#: A wide terminal so two side-by-side parity tiles lay out unwrapped.
_SIZE = (160, 40)

#: The two waves the two seeded executor sessions scope to.
_WAVE_A = "P01-I01-W01"
_WAVE_B = "P01-I01-W02"

#: The new mutating binding the Watch screen adds: cancel the watched session.
_CANCEL_KEY = "k"
_CANCEL_ACTION = "cancel_session"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry + daemon-socket resolution at an empty ``tmp_path`` home.

    With no socket under the patched home the App's binder never connects the
    daemon push leg, so the poll-backstop probe exercises the real mtime-poll
    path (push disabled) rather than racing a live push.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never resolve a real daemon socket; drive a tight poll cadence."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    # A tight poll cadence so tick_poll_backstop wakes the binder loop fast.
    monkeypatch.setenv("EAWF_POLL_INTERVAL_S", "0.02")


def _session(
    sid: str,
    *,
    scope_id: str,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
) -> AgentSession:
    """Build an executor agent-session row for the fleet enumerator."""
    return AgentSession(
        id=sid,
        role=role,
        runtime="claude",
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


def _two_executor_state() -> State:
    """A state with two ACTIVE executor sessions (the side-by-side fleet)."""
    return _state(
        sessions={
            "S-1": _session("S-1", scope_id=_WAVE_A),
            "S-2": _session("S-2", scope_id=_WAVE_B),
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
# push_state_revision -- a pushed revision re-renders without an app restart
# --------------------------------------------------------------------------


def test_push_state_revision_grows_fleet_pane_without_restart(tmp_path: Path) -> None:
    """A pushed state revision re-renders the fleet pane in place, no restart.

    The push-leg half of the live-behaviour criterion: starting from ONE ACTIVE
    executor (the single-session zoom, no grid), pushing a fresh revision that
    adds a second ACTIVE executor through the App ``_on_state`` push hook grows
    the body into the side-by-side parity grid -- the same App instance, no
    restart.
    """
    one = _state(sessions={"S-1": _session("S-1", scope_id=_WAVE_A)})
    state_path = _write_state(tmp_path, one)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22 default is the roster)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            # One ACTIVE executor -> single-session zoom, no parity grid yet.
            assert len(screen.query(WatchGrid)) == 0
            # Push a fresh revision adding a second ACTIVE executor.
            await push_state_revision(pilot, _two_executor_state())
            # The same App re-rendered into the side-by-side parity grid.
            assert app.screen is screen
            assert len(screen.query(WatchGrid)) == 1

    asyncio.run(body())


def test_push_state_revision_rejects_app_without_push_hook() -> None:
    """The push probe raises against a bare host with no ``_on_state`` hook.

    The error path: a host App that is not an EaApp exposes no push seam, so
    the probe surfaces an AttributeError rather than silently no-opping (which
    would let a test green against an un-pushed frame).
    """
    from textual.app import App

    class _BareApp(App[None]):
        def compose(self) -> object:
            return iter(())

    async def body() -> None:
        app = _BareApp()
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            with pytest.raises(AttributeError, match="no _on_state push hook"):
                await push_state_revision(pilot, _two_executor_state())

    asyncio.run(body())


# --------------------------------------------------------------------------
# tick_poll_backstop -- the always-on poll refreshes with push disabled
# --------------------------------------------------------------------------


def test_tick_poll_backstop_refreshes_with_push_disabled(tmp_path: Path) -> None:
    """With no daemon socket, the mtime-poll backstop still refreshes the fleet.

    The poll-leg half of the live-behaviour criterion: the App's daemon push is
    dark (no socket under the patched home), so the only refresh path is the
    binder's always-on mtime-poll loop. Advancing the on-disk ``state.json`` to
    a two-executor fleet and letting the poll loop tick grows the body into the
    side-by-side parity grid -- the staleness backstop the project's TUI lesson
    pins beside the push.
    """
    one = _state(sessions={"S-1": _session("S-1", scope_id=_WAVE_A)})
    state_path = _write_state(tmp_path, one)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await pilot.press("g")  # opt into the parity grid (W22 default is the roster)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            assert len(screen.query(WatchGrid)) == 0
            # Advance the on-disk state (NO push) and let the poll loop fire.
            await tick_poll_backstop(pilot, state_path, _two_executor_state())
            assert app.screen is screen
            assert len(screen.query(WatchGrid)) == 1

    asyncio.run(body())


def test_tick_poll_backstop_advances_the_on_disk_mtime(tmp_path: Path) -> None:
    """The poll probe writes the fresh revision to the fixture file it polls.

    Boundary check on the probe's side effect: the only mutation the poll
    backstop probe performs is the fixture write (the daemon stays the sole
    live-state writer), so the bytes on disk reflect the pushed revision and
    its mtime advances past the seed read.
    """
    one = _state(sessions={"S-1": _session("S-1", scope_id=_WAVE_A)})
    state_path = _write_state(tmp_path, one)
    before = state_path.stat().st_mtime

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            await tick_poll_backstop(pilot, state_path, _two_executor_state())

    asyncio.run(body())
    after_state = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert set(after_state.agent_sessions) == {"S-1", "S-2"}
    assert state_path.stat().st_mtime >= before


# --------------------------------------------------------------------------
# mutating_action_keys_resolve -- each new mutating key is a real handler
# --------------------------------------------------------------------------


def test_mutating_action_keys_resolve_reports_real_handler(tmp_path: Path) -> None:
    """The new cancel key resolves to a non-no-op ``action_*`` on the screen.

    The action-key half of the live-behaviour criterion: the Watch screen's new
    mutating binding (``k`` -> ``cancel_session``) maps to a real
    ``action_cancel_session`` handler on the screen namespace, so the key fires
    a real action rather than a silent no-op. Textual's own ``run_action``
    dispatcher confirms the end-to-end resolution + fire.
    """
    state_path = _write_state(tmp_path, _two_executor_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            resolved = mutating_action_keys_resolve(
                bindings=[(_CANCEL_KEY, _CANCEL_ACTION)],
                namespace=screen,
            )
            # The new mutating key resolves to a real handler, not a no-op.
            assert resolved == {_CANCEL_KEY: True}
            # End-to-end: Textual's dispatcher resolves + fires the action.
            assert await app.run_action("cancel_session", default_namespace=screen) is True

    asyncio.run(body())


def test_mutating_action_keys_resolve_flags_missing_handler(tmp_path: Path) -> None:
    """A bound key whose ``action_*`` is absent is reported as a no-op.

    The error path: a declared binding naming a handler the namespace does not
    define is the silent-no-op bug the project's TUI lesson pins; the probe
    reports it ``False`` so a test catches the dead key.
    """
    state_path = _write_state(tmp_path, _two_executor_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, AgentWatchModeScreen)
            resolved = mutating_action_keys_resolve(
                bindings=[
                    (_CANCEL_KEY, _CANCEL_ACTION),
                    ("z", "definitely_not_an_action"),
                ],
                namespace=screen,
            )
            assert resolved == {_CANCEL_KEY: True, "z": False}

    asyncio.run(body())


def test_mutating_action_keys_resolve_empty_bindings_is_empty() -> None:
    """Zero declared bindings yields an empty resolution map (boundary)."""

    class _Sentinel:
        pass

    # No App needed: the probe only attribute-probes the namespace.
    resolved = mutating_action_keys_resolve(bindings=[], namespace=_Sentinel())  # type: ignore[arg-type]
    assert resolved == {}
