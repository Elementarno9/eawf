"""Smoke tests for the C06 Textual ``EaApp`` scaffold (P26-W16).

Covers the scaffold contract this wave establishes: read-only state
load, scope-name resolution, breadcrumb rendering, ``EaApp``
construction per scope, and a Pilot-driven first-paint that confirms the
``Eä`` brand reaches the rendered screen. The concrete per-scope
compositions land in later waves; these tests pin the shell so those
waves have a stable base.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

import orjson
import pytest
from textual.logging import TextualHandler

from eawf.kernel.state.enums import ScopeKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.tui.app import (
    BRAND,
    DEFAULT_PROJECT_CODE,
    DEGRADED_BANNER_HIDDEN_CLASS,
    DEGRADED_BANNER_ID,
    STALE_SCHEMA_BANNER_HIDDEN_CLASS,
    STALE_SCHEMA_BANNER_ID,
    EaApp,
    Header,
    RepoScreen,
    UserScreen,
    WorkspaceScreen,
    _breadcrumb,
    _restore_root_logging,
    _swap_root_logging_to_textual,
    resolve_scope,
)
from eawf.surfaces.tui.snapshot import capture_screen_text
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks, load_state
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"


def _load_fixture(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _live_event() -> Envelope:
    return Envelope(
        id="EV-live",
        kind="event",
        scope_id="urn:eawf:v1:state:QR",
        created_at="2026-05-27T00:00:00Z",
        updated_at=None,
        summary="live event",
        payload={
            "timestamp": "2026-05-27T00:00:00Z",
            "event_type": "test",
            "actor": "daemon",
            "command": "test",
            "args_hash": "",
            "status": "ok",
            "message": "live",
        },
    )


class _PushReader:
    def __init__(self, push: bytes) -> None:
        self._lines = [push, b""]

    def readline(self) -> bytes:
        return self._lines.pop(0)


class _FakeDaemonClient:
    def __init__(self, push: bytes, calls: list[tuple[str, dict[str, object]]]) -> None:
        self._reader = _PushReader(push)
        self._calls = calls

    def __enter__(self) -> _FakeDaemonClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._calls.append((method, params))
        return {"ok": True}


class _SilentStreamReader:
    """Reader that models a connected-but-silent push stream.

    ``readline`` never returns an ``event.push`` frame and never returns
    EOF — it yields a non-push heartbeat line forever (with a tiny sleep
    so the off-thread subscribe loop does not busy-spin). This keeps the
    subscription "connected" (no error, no stream-end) while delivering
    zero state refreshes, exactly the stalled-push case the backstop
    poll must cover.
    """

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self._heartbeat = orjson.dumps({"jsonrpc": "2.0", "method": "noop", "params": {}}) + b"\n"

    def readline(self) -> bytes:
        # Return EOF once the test is tearing down so the subscribe
        # thread can exit cleanly instead of looping forever.
        if self._stop.is_set():
            return b""
        time.sleep(0.005)
        return self._heartbeat


class _SilentDaemonClient:
    """Daemon client whose subscribe call connects but never pushes state."""

    def __init__(self, calls: list[tuple[str, dict[str, object]]], stop: threading.Event) -> None:
        self._reader = _SilentStreamReader(stop)
        self._calls = calls

    def __enter__(self) -> _SilentDaemonClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._calls.append((method, params))
        return {"ok": True}


# --------------------------------------------------------------------------
# load_state — read-only, error-tolerant
# --------------------------------------------------------------------------


def test_load_state_none_path_returns_none() -> None:
    assert load_state(None) is None


def test_load_state_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_state(tmp_path / "absent" / "state.json") is None


def test_load_state_corrupt_json_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_state(bad) is None


def test_load_state_schema_mismatch_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "state.json"
    bad.write_text(orjson.dumps({"schema_version": "1.0"}).decode(), encoding="utf-8")
    assert load_state(bad) is None


def test_load_state_valid_repo_fixture() -> None:
    state = load_state(_EMPTY_REPO)
    assert state is not None
    assert state.scope_kind is ScopeKind.REPO


def test_load_state_does_not_mutate_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(_EMPTY_REPO.read_bytes())
    before = target.read_bytes()
    load_state(target)
    assert target.read_bytes() == before


# --------------------------------------------------------------------------
# resolve_scope — scope_kind -> EaApp scope name
# --------------------------------------------------------------------------


def test_resolve_scope_repo() -> None:
    assert resolve_scope(ScopeKind.REPO) == "repo"


def test_resolve_scope_workspace() -> None:
    assert resolve_scope(ScopeKind.WORKSPACE) == "workspace"


# --------------------------------------------------------------------------
# _breadcrumb — brand-less crumb with sane fallback
# --------------------------------------------------------------------------


def test_breadcrumb_none_state_falls_back_to_default_code() -> None:
    assert _breadcrumb(None) == DEFAULT_PROJECT_CODE


def test_breadcrumb_repo_fixture_includes_scope_and_code() -> None:
    crumb = _breadcrumb(_load_fixture(_EMPTY_REPO))
    assert "repo" in crumb
    assert "QR" in crumb


def test_breadcrumb_workspace_fixture_uses_default_code_when_no_project() -> None:
    crumb = _breadcrumb(_load_fixture(_WORKSPACE))
    assert "workspace" in crumb
    assert DEFAULT_PROJECT_CODE in crumb


# --------------------------------------------------------------------------
# EaApp construction — per scope
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "screen_cls"),
    [
        ("repo", RepoScreen),
        ("workspace", WorkspaceScreen),
        ("user", UserScreen),
    ],
)
def test_eaapp_constructs_per_scope(scope: str, screen_cls: type) -> None:
    app = EaApp(scope=scope, state_path=None)  # type: ignore[arg-type]
    assert app._scope == scope
    assert app.SCREENS[scope] is screen_cls


def test_eaapp_css_path_is_tcss() -> None:
    assert EaApp.CSS_PATH == "theme.tcss"


def test_eaapp_arrow_and_vim_bindings_present() -> None:
    keys = {b.key for b in EaApp.BINDINGS}  # type: ignore[union-attr]
    # Vim aliases declared app-wide; arrow keys are bound per-screen but
    # the scope-switch + quit chords live here.
    assert {"h", "j", "k", "l"} <= keys
    # Scope switch is the raw w/r/u keys (the W32 keybinding fix); the
    # ctrl+ chords remain as hidden muscle-memory aliases.
    assert {"w", "r", "u"} <= keys
    assert {"ctrl+r", "ctrl+w", "ctrl+u"} <= keys


def test_eaapp_raw_scope_switch_bindings_target_switch_scope() -> None:
    actions = {b.key: b.action for b in EaApp.BINDINGS}  # type: ignore[union-attr]
    assert actions["w"] == "switch_scope('workspace')"
    assert actions["r"] == "switch_scope('repo')"
    assert actions["u"] == "switch_scope('user')"


# --------------------------------------------------------------------------
# StateBinding — read-only initial load via callbacks
# --------------------------------------------------------------------------


def test_state_binding_connect_pushes_initial_state_and_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_state: list[State] = []
    seen_degraded: list[bool] = []

    async def on_state(s: State) -> None:
        seen_state.append(s)

    async def on_degraded(d: bool) -> None:
        seen_degraded.append(d)

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            poll_interval_s=0.01,
            daemon_failure_threshold=1,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: False)
        await binder.connect()
        await binder.disconnect()

    asyncio.run(body())
    assert len(seen_state) == 1
    assert seen_state[0].scope_kind is ScopeKind.REPO
    # Fallback leg active until the daemon-push leg is wired.
    assert seen_degraded == [True]


def test_state_binding_poll_loop_survives_stat_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOCTOU ``stat()`` failure mid-poll is swallowed; the poll loop survives.

    Simulates the state file vanishing between the ``is_file()`` check and the
    ``stat()`` call inside ``_poll_loop`` (``is_file()`` lies ``True``,
    ``stat()`` raises). Without the guard the loop coroutine would raise and the
    poll task would die; with it the tick is skipped and the task stays alive.
    """

    async def _noop_state(_s: State) -> None:
        return None

    async def _noop_degraded(_d: bool) -> None:
        return None

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=_noop_state, on_degraded=_noop_degraded),
            poll_interval_s=0.01,
            daemon_failure_threshold=1,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: False)
        await binder.connect()
        # TOCTOU: is_file() sees the file but the subsequent stat() loses it.
        monkeypatch.setattr(Path, "is_file", lambda _self: True)

        def _boom_stat(_self: Path, *_a: object, **_k: object) -> object:
            raise FileNotFoundError("state.json vanished after is_file()")

        monkeypatch.setattr(Path, "stat", _boom_stat)
        await asyncio.sleep(0.05)  # several poll ticks under the failing stat()
        assert binder._poll_task is not None
        assert not binder._poll_task.done()  # the loop swallowed the OSError
        await binder.disconnect()

    asyncio.run(body())


def test_state_binding_subscribes_via_daemon_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_state: list[State] = []
    seen_degraded: list[bool] = []
    seen_events: list[Envelope] = []
    calls: list[tuple[str, dict[str, object]]] = []
    event = _live_event()
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

    async def on_state(s: State) -> None:
        seen_state.append(s)

    async def on_degraded(d: bool) -> None:
        seen_degraded.append(d)

    async def on_event(e: Envelope) -> None:
        seen_events.append(e)

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(
                on_state=on_state,
                on_degraded=on_degraded,
                on_event=on_event,
            ),
            daemon_client_factory=lambda: _FakeDaemonClient(push, calls),  # type: ignore[arg-type]
            poll_interval_s=0.01,
            daemon_failure_threshold=10,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: True)
        await binder.connect()
        await asyncio.sleep(0.05)
        await binder.disconnect()

    asyncio.run(body())
    # W25: the EVENT subscription is not narrowed by scope_id -- fleet events
    # are wave-/iter-scoped, so a state-URN filter would drop them all.
    assert calls == [
        (
            "state.subscribe",
            {"kinds": ["event"]},
        )
    ]
    assert seen_degraded == []
    assert [e.id for e in seen_events] == ["EV-live"]
    assert len(seen_state) >= 2


def test_state_binding_reconnects_when_push_stream_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_state: list[State] = []
    seen_degraded: list[bool] = []
    seen_events: list[Envelope] = []
    calls: list[tuple[str, dict[str, object]]] = []
    event = _live_event()
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

    async def on_state(s: State) -> None:
        seen_state.append(s)

    async def on_degraded(d: bool) -> None:
        seen_degraded.append(d)

    async def on_event(e: Envelope) -> None:
        seen_events.append(e)

    async def body() -> None:
        def _daemon_client_factory() -> _FakeDaemonClient:
            return _FakeDaemonClient(push, calls)  # type: ignore[arg-type]

        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(
                on_state=on_state,
                on_degraded=on_degraded,
                on_event=on_event,
            ),
            daemon_client_factory=_daemon_client_factory,
            poll_interval_s=0.01,
            daemon_failure_threshold=1,
            daemon_probe_interval_s=0.01,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: len(calls) <= 1)
        # W04: disable the reconnect throttle so the on-drop reconnect fires
        # within the tight test window (production throttles to ~5s).
        binder._reconnect_min_interval = 0.0
        await binder.connect()
        await asyncio.sleep(0.2)
        await binder.disconnect()

    asyncio.run(body())
    # W04: the first subscribe carries no cursor; the reconnect resumes from the
    # last delivered event id (since=EV-live) so it does not re-request backlog.
    # W25: neither subscribe narrows by scope_id (fleet events are wave-scoped).
    first = ("state.subscribe", {"kinds": ["event"]})
    reconnect = (
        "state.subscribe",
        {"kinds": ["event"], "since": "EV-live"},
    )
    assert calls == [first, reconnect]
    assert seen_events
    assert [e.id for e in seen_events] == ["EV-live", "EV-live"]
    assert any(v is True for v in seen_degraded)
    assert any(v is False for v in seen_degraded)
    assert len(seen_state) >= 3


def test_state_binding_poll_backstop_refreshes_when_push_stream_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A silent push stream still picks up an mtime advance via the backstop.

    Regression for the stale-roadmap bug: in push mode the binder used to
    stop the mtime-poll loop the moment the subscription connected, so a
    push stream that connected but then delivered no frame (a stall, an
    un-pushed change, or a daemon-side ``scope_id`` filter drop) left the
    bound state frozen until the operator restarted the TUI. The fix keeps
    a slow mtime-poll loop running as a backstop even while push is
    connected; this test drives a connected-but-silent stream, advances
    ``state.json`` on disk, and asserts a fresh ``on_state`` is still
    delivered within the backstop interval -- with the daemon never
    flagged degraded (the backstop is silent, not a degraded signal).
    """
    state_file = tmp_path / "state.json"
    state_file.write_bytes(_EMPTY_REPO.read_bytes())
    seen_state: list[State] = []
    seen_degraded: list[bool] = []
    calls: list[tuple[str, dict[str, object]]] = []
    stop = threading.Event()

    async def on_state(s: State) -> None:
        seen_state.append(s)

    async def on_degraded(d: bool) -> None:
        seen_degraded.append(d)

    async def body() -> None:
        binder = StateBinding(
            state_path=state_file,
            callbacks=StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            daemon_client_factory=lambda: _SilentDaemonClient(calls, stop),  # type: ignore[arg-type]
            poll_interval_s=0.01,
            daemon_failure_threshold=10,
            daemon_probe_interval_s=0.01,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: True)
        await binder.connect()
        # Push connected (subscribe issued) but the stream is silent: only
        # the initial connect() load has landed so far.
        await asyncio.sleep(0.05)
        assert calls and calls[0][0] == "state.subscribe"
        baseline = len(seen_state)

        # A lifecycle change lands on disk (new wave/iter) -> mtime advances.
        # The push stream stays silent, so only the backstop can pick it up.
        bumped = orjson.loads(state_file.read_bytes())
        bumped["updated_at"] = "2026-05-28T00:00:00Z"
        state_file.write_bytes(orjson.dumps(bumped))
        future = state_file.stat().st_mtime + 5.0
        os.utime(state_file, (future, future))

        # Within a few backstop ticks the refreshed state is delivered.
        deadline = time.monotonic() + 2.0
        while len(seen_state) <= baseline and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        stop.set()
        await binder.disconnect()

    asyncio.run(body())
    assert len(seen_state) >= 2  # initial connect load + the backstop refresh
    assert seen_state[-1].updated_at.year == 2026
    assert seen_state[-1].updated_at.month == 5
    assert seen_state[-1].updated_at.day == 28
    # The backstop is a silent safety net, never a degraded-mode signal.
    assert seen_degraded == []


def test_state_binding_process_daemon_probe_debounces_degraded_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def _track_subscribe() -> None:
        seen.append("subscribe")

    async def _track_fallback() -> None:
        seen.append("fallback")

    async def on_state(_s: State) -> None:
        return None

    async def on_degraded(_d: bool) -> None:
        return None

    async def body() -> None:
        probes = iter([False, False, True, False, False, False])

        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            daemon_failure_threshold=3,
        )
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: next(probes))
        monkeypatch.setattr(binder, "_start_subscribe_loop", _track_subscribe)
        monkeypatch.setattr(binder, "_start_poll_fallback", _track_fallback)

        await binder._process_daemon_probe()
        await binder._process_daemon_probe()
        await binder._process_daemon_probe()
        await binder._process_daemon_probe()
        assert seen == ["subscribe"]
        await binder._process_daemon_probe()
        await binder._process_daemon_probe()
        assert seen == ["subscribe", "fallback"]

    asyncio.run(body())


def test_state_binding_clear_degraded_only_after_subscription_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_degraded: list[bool] = []

    async def on_state(_s: State) -> None:
        return None

    async def on_degraded(degraded: bool) -> None:
        seen_degraded.append(degraded)

    async def body() -> None:
        binder = StateBinding(
            state_path=_EMPTY_REPO,
            callbacks=StateBindingCallbacks(on_state=on_state, on_degraded=on_degraded),
            daemon_failure_threshold=1,
        )
        await binder._set_degraded(True)
        assert seen_degraded == [True]

        subscribed: list[bool] = []
        monkeypatch.setattr(binder, "_daemon_socket_available", lambda: True)

        async def _track_subscribe() -> None:
            subscribed.append(True)

        monkeypatch.setattr(binder, "_start_subscribe_loop", _track_subscribe)
        await binder._process_daemon_probe()
        assert subscribed == [True]
        assert seen_degraded == [True]

        await binder._on_subscription_connected()
        assert seen_degraded == [True, False]
        await binder.disconnect()

    asyncio.run(body())


def test_eaapp_degraded_banner_stays_mounted_and_toggles_visibility() -> None:

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.screen.query_one(f"#{DEGRADED_BANNER_ID}")
            banner_id = id(banner)
            assert len(app.screen.query(f".{DEGRADED_BANNER_ID}")) == 1
            assert banner.styles.height.value == 1
            assert banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            await app._on_degraded(True)
            await pilot.pause()
            banner = app.screen.query_one(f"#{DEGRADED_BANNER_ID}")
            assert id(banner) == banner_id
            assert len(app.screen.query(f".{DEGRADED_BANNER_ID}")) == 1
            assert banner.styles.height.value == 1
            assert not banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            # The reskinned banner leads with the FAIL sigil + calm copy, then
            # trails the diagnostics the normaliser keys on.
            rendered = str(banner.render())
            assert glyph(Sigil.FAILED, mode="unicode") in rendered
            assert "daemon unreachable, reconnecting" in rendered
            assert "daemon socket unavailable" in rendered

            await app._on_degraded(False)
            await pilot.pause()
            banner = app.screen.query_one(f"#{DEGRADED_BANNER_ID}")
            assert id(banner) == banner_id
            assert len(app.screen.query(f".{DEGRADED_BANNER_ID}")) == 1
            assert banner.styles.height.value == 1
            assert banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)

    asyncio.run(body())


def test_eaapp_degraded_banner_leads_with_fail_sigil_and_calm_copy() -> None:
    """Daemon unreachable: the banner widget leads with the FAIL sigil + calm copy.

    The W25 close-gate criterion as a NON-normalised Pilot test: the snapshot
    normaliser strips the daemon-state-dependent degraded line, so the
    reskinned banner is pinned by querying the mounted banner widget and
    asserting the FAIL sigil leads its rendered content + the calm
    "daemon unreachable, reconnecting" copy follows. Driven through the same
    ``_on_degraded`` hook the binder fires.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._on_degraded(True)
            await pilot.pause()
            banner = app.screen.query_one(f"#{DEGRADED_BANNER_ID}")
            assert not banner.has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            rendered = str(banner.render())
            # The FAIL sigil (the reskin's terminal cross) LEADS the calm copy.
            mark = glyph(Sigil.FAILED, mode="unicode")
            assert mark in rendered
            assert rendered.index(mark) < rendered.index("daemon unreachable, reconnecting")

    asyncio.run(body())


def test_eaapp_healthy_mounts_neither_banner(tmp_path: Path) -> None:
    """A healthy app (live daemon, current schema) mounts neither banner visibly.

    The W25 close-gate negative: with no degraded flip and a state re-stamped
    to the live schema, the degraded banner and the stale-schema banner are
    both either unmounted or hidden, so a healthy operator sees the clean
    chrome.
    """
    from eawf.surfaces.tui.state_binding import live_schema_version

    payload = orjson.loads(_EMPTY_REPO.read_bytes())
    payload["schema_version"] = live_schema_version()
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(State.model_validate(payload).model_dump(mode="json")))

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.degraded is False
            assert app.stale_schema is False
            degraded = app.screen.query(f"#{DEGRADED_BANNER_ID}")
            if degraded:
                assert degraded.first().has_class(DEGRADED_BANNER_HIDDEN_CLASS)
            stale = app.screen.query(f"#{STALE_SCHEMA_BANNER_ID}")
            if stale:
                assert stale.first().has_class(STALE_SCHEMA_BANNER_HIDDEN_CLASS)

    asyncio.run(body())


def test_eaapp_degraded_banner_message_includes_socket_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime_dir))
    app = EaApp(scope="repo", state_path=_EMPTY_REPO)
    message = app._degraded_banner_message()
    assert "daemon unreachable, reconnecting" in message
    assert "daemon socket unavailable" in message
    assert f"{runtime_dir / 'eawfd.sock'}" in message


# --------------------------------------------------------------------------
# Pilot first-paint smoke — confirms the shell renders the brand
# --------------------------------------------------------------------------


def test_eaapp_first_paint_renders_brand() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test() as pilot:
            await pilot.pause()
            # The real RepoScreen now composes the shared chassis Header.
            header = app.screen.query_one(Header)
            assert BRAND in str(header.render())
            # Plain-text screen capture is the end-to-end paint proof; the
            # SVG export splits the two-tone wordmark across text spans so
            # the contiguous brand pair only survives in the text capture.
            assert BRAND in capture_screen_text(app)

    asyncio.run(body())


# --------------------------------------------------------------------------
# Root-logging swap — no stderr handler bleeds onto the live TUI screen
# --------------------------------------------------------------------------


def _has_terminal_stream_handler() -> bool:
    """Return ``True`` when a root handler still writes to stderr/stdout."""
    root = logging.getLogger()
    return any(
        isinstance(h, logging.StreamHandler) and h.stream in (sys.stderr, sys.stdout)
        for h in root.handlers
    )


@pytest.fixture
def _isolated_root_logging() -> object:
    """Save + restore the real root handler list around a swap test."""
    root = logging.getLogger()
    original = list(root.handlers)
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in original:
        root.addHandler(handler)


def test_swap_root_logging_removes_stderr_handler(_isolated_root_logging: object) -> None:
    """The swap detaches the stderr StreamHandler and installs a TextualHandler."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)
    assert _has_terminal_stream_handler()  # precondition: the leak is present

    _swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()  # no handler writes to the screen
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_swap_root_logging_textual_handler_is_timestamped(
    _isolated_root_logging: object,
) -> None:
    """The installed TextualHandler carries a timestamped (asctime) formatter."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    _swap_root_logging_to_textual()

    textual = next(h for h in root.handlers if isinstance(h, TextualHandler))
    record = logging.LogRecord(
        name="eawf.demo",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="drive armed",
        args=(),
        exc_info=None,
    )
    rendered = textual.format(record)
    # A YYYY-MM-DD timestamp precedes the level so console latency is measurable.
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rendered)
    assert "INFO eawf.demo drive armed" in rendered


def test_swap_root_logging_also_detaches_stdout(_isolated_root_logging: object) -> None:
    """A stdout-targeting StreamHandler is detached too (both terminal streams)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.StreamHandler(stream=sys.stdout))

    _swap_root_logging_to_textual()

    assert not _has_terminal_stream_handler()
    assert any(isinstance(h, TextualHandler) for h in root.handlers)


def test_restore_root_logging_reinstates_prior_handlers(
    _isolated_root_logging: object,
) -> None:
    """Restore reinstates the exact handler list captured before the swap."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    root.addHandler(stderr_handler)

    saved = _swap_root_logging_to_textual()
    assert not _has_terminal_stream_handler()  # swapped out for the run

    _restore_root_logging(saved)

    assert root.handlers == [stderr_handler]  # exact prior list back
    assert _has_terminal_stream_handler()  # scrubbed stderr sink restored
    assert not any(isinstance(h, TextualHandler) for h in root.handlers)
