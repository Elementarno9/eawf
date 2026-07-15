"""Daemonless-UX fixes for P30-I21-W25.

Pins the four fixes that keep the operator surface honest when the daemon is
unreachable and deliver wave-scoped fleet events to the live feed:

1. the EVENT subscription is NOT narrowed to the bound state URN, so
   wave-/iter-scoped fleet events reach the App live-event buffer instead of
   being dropped by a daemon-side ``scope_id`` filter;
2. with no reachable daemon the feed seeds from the on-disk event-store tail
   (``store/event.jsonl``, the same store the ``/events`` overlay reads) and
   shows a "daemon disconnected" connection badge -- the unconditional
   "live feed waiting for events" empty state no longer hides real activity;
3. Escape at a top-level mode screen no longer silently quits the app; and
4. the autopilot frontier renders claim-ready pending waves off ``state.json``
   with no daemon (the frontier is state-driven, not daemon-driven).

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import ScopeKind, StoreKind, WaveStatus
from eawf.kernel.state.models import (
    CurrentPointers,
    Project,
    ProjectStatus,
    State,
    Wave,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    EMPTY_NOTICE,
    FRONTIER_ROW_CLASS,
    AutopilotModeScreen,
)
from eawf.surfaces.tui.modes.feed import (
    FEED_ROW_CLASS,
    FEED_STATUS_DISCONNECTED,
    FeedModeScreen,
    load_recent_envelopes,
)
from eawf.surfaces.tui.scopes import RepoScreen
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

#: The mode digit keys that switch to the feed / autopilot panes.
_FEED_DIGIT = "7"
_AUTOPILOT_DIGIT = "2"

_STATE_URN = "urn:eawf:v1:state:QR"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray ``u`` scope switch (and any registry read) deterministic and
    off the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _wave(wave_id: str, *, status: WaveStatus, deps: list[str] | None = None) -> Wave:
    """Build a wave row for the frontier projection."""
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _state(waves: dict[str, Wave] | None = None) -> State:
    """Build a minimal repo state, optionally with a wave graph."""
    return State.model_validate(
        {
            "schema_version": "1.3",
            "scope_kind": ScopeKind.REPO.value,
            "urn": _STATE_URN,
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
            "fleet_run": None,
            "phases": {},
            "iters": {},
            "waves": (
                {wid: w.model_dump(mode="json") for wid, w in waves.items()}
                if waves is not None
                else {}
            ),
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _claim_ready_state() -> State:
    """Build a state whose frontier carries two claim-ready pending waves.

    W01 is CLOSED; W02 is PENDING with W01 (CLOSED) as its only dep, so W02 is
    claim-ready. A second iter's W05 (dep W01 closed) is also claim-ready. The
    ready frontier is therefore ``(W02, W05)`` -- computed purely off state, so
    it renders with no daemon reachable.
    """
    waves = {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        "P01-I02-W05": Wave(
            id="P01-I02-W05",
            iter_id="P01-I02",
            title="Wave P01-I02-W05",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            opened_at=_T0,
        ),
    }
    return _state(waves)


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _event(event_id: str, *, scope_id: str | None, summary: str) -> Envelope:
    """Build a minimal event-kind envelope."""
    return Envelope(
        id=event_id,
        kind="event",  # type: ignore[arg-type]
        scope_id=scope_id,
        created_at=_T0,
        updated_at=None,
        summary=summary,
        payload={"event_type": "state.mutate.wave_close", "status": "closed"},
    )


def _write_event_store(state_path: Path, envelopes: tuple[Envelope, ...]) -> Path:
    """Write *envelopes* as JSONL to the state's sibling event store."""
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        "\n".join(env.model_dump_json() for env in envelopes) + "\n", encoding="utf-8"
    )
    return event_path


class _PushReader:
    """A makefile-style reader yielding one push frame then EOF."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = [*frames, b""]

    def readline(self) -> bytes:
        if not self._frames:
            return b""
        return self._frames.pop(0)


class _FakeDaemonClient:
    """A drop-in for :class:`DaemonClient` that streams canned push frames."""

    def __init__(self, frames: list[bytes], calls: list[tuple[str, dict[str, object]]]) -> None:
        self._reader = _PushReader(frames)
        self._calls = calls

    def __enter__(self) -> _FakeDaemonClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._calls.append((method, params))
        return {"ok": True}


# --------------------------------------------------------------------------
# Criterion 1 -- EVENT subscription is not scope-narrowed
# --------------------------------------------------------------------------


def test_subscribe_params_omit_scope_id_for_event_stream() -> None:
    """The EVENT subscribe carries the kinds but no ``scope_id`` narrowing."""

    async def _noop_state(_s: object) -> None:
        return None

    async def _noop_degraded(_d: bool) -> None:
        return None

    binding = StateBinding(
        None, StateBindingCallbacks(on_state=_noop_state, on_degraded=_noop_degraded)
    )
    params = binding._subscribe_params()
    assert "scope_id" not in params
    assert params["kinds"] == ["event"]


def test_wave_scoped_push_reaches_app_live_event_buffer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wave-scoped envelope pushed over the stream lands in the App buffer.

    Before the fix the binding narrowed the subscribe to the state URN, so the
    daemon bus dropped every wave-scoped fleet event and none reached the feed.
    This drives the real off-thread subscribe loop against a fake daemon client
    streaming a wave-scoped (``scope_id="P30-I21-W25"``) envelope and asserts it
    lands in ``app.live_event_buffer`` -- and that the subscribe call carried no
    ``scope_id`` filter.
    """
    state_path = _write_state(tmp_path, _state())
    event = _event("EV-wave", scope_id="P30-I21-W25", summary="wave P30-I21-W25 closed")
    push = (
        orjson.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event.push",
                "params": {"event": event.model_dump(mode="json")},
            }
        )
        + b"\n"
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def body() -> None:
        from eawf.surfaces.tui import state_binding as sb

        monkeypatch.setattr(sb.StateBinding, "_daemon_socket_available", lambda _self: True)
        monkeypatch.setattr(sb, "DaemonClient", lambda *a, **k: _FakeDaemonClient([push], calls))

        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            for _ in range(20):
                await pilot.pause()
                if any(e.id == "EV-wave" for e in app.live_event_buffer):
                    break
            await settle_screen(pilot)
            assert any(e.id == "EV-wave" for e in app.live_event_buffer)

    asyncio.run(body())
    assert calls and calls[0][0] == "state.subscribe"
    assert "scope_id" not in calls[0][1]


# --------------------------------------------------------------------------
# Criterion 2 -- daemonless feed seeds the file tail + shows a disconnected badge
# --------------------------------------------------------------------------


def test_load_recent_envelopes_reads_tail_skips_malformed(tmp_path: Path) -> None:
    """The tail reader validates real rows and skips malformed / blank lines."""
    state_path = _write_state(tmp_path, _state())
    good = _event("EV-1", scope_id="P01-I01-W02", summary="wave closed")
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(good.model_dump_json() + "\n" + "not json\n" + "\n", encoding="utf-8")
    rows = load_recent_envelopes(event_path)
    assert tuple(e.id for e in rows) == ("EV-1",)


def test_load_recent_envelopes_missing_store_is_empty(tmp_path: Path) -> None:
    """A missing event store yields no rows rather than raising."""
    assert load_recent_envelopes(tmp_path / "store" / "event.jsonl") == ()


def test_feed_seeds_file_tail_and_shows_disconnected_badge_daemonless(tmp_path: Path) -> None:
    """Daemonless: the feed renders the event-store tail plus a disconnected badge.

    With the daemon unreachable the App live ring stays empty, so the feed
    seeds the newest events off ``store/event.jsonl`` and reveals the
    "daemon disconnected" connection badge -- real recent activity instead of
    an unconditional "waiting for events" empty state.
    """
    state_path = _write_state(tmp_path, _state())
    _write_event_store(
        state_path,
        (
            _event("EV-a", scope_id="P01-I01-W01", summary="wave P01-I01-W01 closed"),
            _event("EV-b", scope_id="P01-I01-W02", summary="wave P01-I01-W02 claimed"),
        ),
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Force the daemonless (degraded) state, as the socket probe would.
            await app._on_degraded(True)
            await settle_screen(pilot, quiesce=False)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot, quiesce=False)
            pane = app.screen
            assert isinstance(pane, FeedModeScreen)
            # Both on-disk tail rows render (no unconditional empty state).
            assert len(pane.query(f".{FEED_ROW_CLASS}")) == 2
            frame = normalize_snapshot(capture_screen_text(app))
            assert "wave P01-I01-W01 closed" in frame
            assert "wave P01-I01-W02 claimed" in frame
            # The connection indicator flags the daemonless state.
            assert FEED_STATUS_DISCONNECTED in frame

    asyncio.run(body())


def test_feed_connection_badge_hidden_when_connected(tmp_path: Path) -> None:
    """While the daemon is reachable the connection badge carries no chrome."""
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, FeedModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert FEED_STATUS_DISCONNECTED not in frame

    asyncio.run(body())


# --------------------------------------------------------------------------
# Criterion 3 -- Escape does not quit; frontier renders daemonless
# --------------------------------------------------------------------------


def test_escape_does_not_exit_from_home(tmp_path: Path) -> None:
    """A stray Escape at the top-level Home screen never quits the app.

    The app-tier Escape->quit binding is removed, so Escape is a no-op at a
    top-level mode screen; the app stays running and still responds to input.
    """
    state_path = _write_state(tmp_path, _state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert isinstance(app.screen, RepoScreen)
            await pilot.press("escape")
            await settle_screen(pilot)
            # The app is alive and still on the Home scope screen.
            assert app.is_running
            assert isinstance(app.screen, RepoScreen)
            # ... and it still responds to a following keypress.
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, AutopilotModeScreen)

    asyncio.run(body())


def test_autopilot_frontier_renders_claim_ready_waves_without_daemon(tmp_path: Path) -> None:
    """Claim-ready pending waves render on the frontier with no daemon.

    The frontier is derived from ``state.json`` (deps-satisfied pending waves),
    not the daemon, so a degraded (daemonless) app still lists the claim-ready
    waves rather than reporting "no ready waves".
    """
    state_path = _write_state(tmp_path, _claim_ready_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await app._on_degraded(True)  # daemonless
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            rows = pane.query(f".{FRONTIER_ROW_CLASS}")
            assert len(rows) == 2  # the two claim-ready waves
            frame = normalize_snapshot(capture_screen_text(app))
            assert "P01-I01-W02" in frame
            assert "P01-I02-W05" in frame
            assert EMPTY_NOTICE not in frame

    asyncio.run(body())
