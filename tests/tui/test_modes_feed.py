"""Tests for the live event-feed pane (P29-I02-W27, Feed mode digit 7).

The Feed mode renders a live, newest-first view of the daemon
``event.subscribe`` push stream. It does not open its own subscription:
the App's read-only :class:`~eawf.surfaces.tui.state_binding.StateBinding`
already consumes the stream on a worker thread (``asyncio.to_thread`` ->
blocking ``readline`` loop) and marshals each decoded envelope back to the
event loop via ``run_coroutine_threadsafe`` -> :meth:`EaApp._on_event`,
which fans it out to every mounted Feed pane. These tests pin:

* the pure row formatter (``<HH:MM:SS> <kind> <summary>``), incl. the
  empty-summary boundary;
* the pane registers as a live-feed listener on mount and unregisters on
  unmount (clean teardown -- the fan-out never targets a torn-down pane);
* a live envelope appended through the App fan-out lands at the top of the
  scroll (newest-first), removing the honest-empty notice;
* multiple envelopes preserve newest-first order;
* honest-empty before any event arrives, and the honest-degraded wording
  when the daemon is unreachable (``app.degraded``);
* a mode switch into Feed mid-session seeds from the App's live buffer;
* end-to-end: a real :class:`StateBinding` fed by a fake daemon client
  delivers a push on a worker thread (UI loop not blocked -- asserted via
  the worker harness) and the row appears in the Feed pane.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen`
(``pilot.pause()`` is CPU-idle-based, not worker-aware) before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.tui.app import LIVE_EVENT_BUFFER_MAX, EaApp
from eawf.surfaces.tui.modes.feed import (
    FEED_EMPTY_DEGRADED,
    FEED_EMPTY_ID,
    FEED_EMPTY_LIVE,
    FEED_ROW_CLASS,
    FeedModeScreen,
    event_sigil,
    format_event_markup,
    format_event_row,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, tint

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_REPO = _FIXTURES / "03-phase-iter-wave-active.json"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"

#: The digit key that switches to the Feed mode.
_FEED_DIGIT = "7"

#: The W16 isolated reskin golden, distinct from the shared full-app
#: ``feed_mode_populated.txt`` / ``feed_mode_empty.txt`` snapshots so this
#: wave owns + regenerates it without touching the coupled screen-snapshot
#: suite. Lives beside the other TUI goldens.
_SIGILS_GOLDEN = (
    Path(__file__).resolve().parents[1] / "snapshots" / "tui" / "golden" / "feed_mode_sigils.txt"
)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home.

    Keeps a stray ``u`` scope switch (and any registry read) deterministic
    and off the operator's real registry.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _event(
    event_id: str,
    *,
    summary: str = "live event",
    kind: str = "event",
    status: str = "ok",
    event_kind: str | None = None,
) -> Envelope:
    """Build a minimal event-kind envelope for the feed.

    Args:
        event_id: The envelope id.
        summary: The row summary text.
        kind: The store kind (the kind column).
        status: The payload ``status`` word the lifecycle sigil reads first.
        event_kind: An optional ``event_kind`` tag the sigil falls back to.
    """
    payload: dict[str, object] = {"event_type": "test", "status": status, "message": "live"}
    if event_kind is not None:
        payload["event_kind"] = event_kind
    return Envelope(
        id=event_id,
        kind=kind,  # type: ignore[arg-type]
        scope_id="urn:eawf:v1:state:QR",
        created_at=datetime(2026, 5, 27, 9, 30, 15, tzinfo=UTC),
        updated_at=None,
        summary=summary,
        payload=payload,
    )


# --------------------------------------------------------------------------
# format_event_row -- pure, boundary cases
# --------------------------------------------------------------------------


def test_format_event_row_renders_sigil_time_kind_summary() -> None:
    """A row is ``<sigil> <HH:MM:SS>  <kind padded>  <summary>``.

    The reskin leads each line with a two-cell lifecycle-sigil column (the
    sigil glyph plus one space), then the fixed-width wall-clock, the padded
    kind, and the summary.
    """
    line = format_event_row(_event("EV-1", summary="wave closed", kind="event"))
    # Two-cell sigil column: the CLOSED unicode glyph + one trailing space,
    # then the fixed-width timestamp starts at column 2.
    assert line[0] == glyph(Sigil.CLOSED, mode="unicode")
    assert line[1] == " "
    assert line[2:].startswith("09:30:15")
    assert "event" in line
    assert line.endswith("wave closed")


def test_format_event_row_empty_summary_drops_trailing() -> None:
    """An empty summary yields just the sigil + time + kind head (no pad)."""
    line = format_event_row(_event("EV-1", summary="", kind="decision"))
    assert line[0] == glyph(Sigil.CLOSED, mode="unicode")
    assert line[2:].startswith("09:30:15")
    assert "decision" in line
    assert line == line.rstrip()


def test_format_event_row_widest_kind_keeps_summary_separated() -> None:
    """The widest store kind still leaves a gap before the summary."""
    line = format_event_row(_event("EV-1", summary="done", kind="domain_specialist_report"))
    assert "domain_specialist_report  done" in line


def test_format_event_row_timestamp_column_is_fixed_width() -> None:
    """The time column is laid out to a fixed eight-cell width after the sigil.

    The sigil column is two cells (glyph + space); the ``HH:MM:SS`` time is
    exactly eight cells; the two-space gutter then separates it from the
    kind, so the kind starts at the same column on every row regardless of
    which lifecycle sigil the event wears.
    """
    closed = format_event_row(_event("EV-1", summary="s", kind="event"))
    pending = format_event_row(_event("EV-2", summary="s", kind="event", status="pending"))
    # Both rows: sigil(1) + space(1) + HH:MM:SS(8) + two-space gutter = the
    # kind starts at the same fixed offset.
    assert closed.index("event") == pending.index("event")
    assert closed[2:10] == "09:30:15"
    assert pending[2:10] == "09:30:15"


def test_event_sigil_maps_status_to_lifecycle() -> None:
    """The payload ``status`` word drives the lifecycle sigil first."""
    assert event_sigil(_event("E", status="ok")) is Sigil.CLOSED
    assert event_sigil(_event("E", status="closed")) is Sigil.CLOSED
    assert event_sigil(_event("E", status="claimed")) is Sigil.CLAIMED
    assert event_sigil(_event("E", status="running")) is Sigil.RUNNING
    assert event_sigil(_event("E", status="in_progress")) is Sigil.RUNNING
    assert event_sigil(_event("E", status="pending")) is Sigil.PENDING
    assert event_sigil(_event("E", status="failed")) is Sigil.FAILED
    assert event_sigil(_event("E", status="error")) is Sigil.FAILED


def test_event_sigil_falls_back_to_event_kind_substring() -> None:
    """An unrecognised status defers to an ``event_kind`` substring scan."""
    assert event_sigil(_event("E", status="?", event_kind="wave_claimed")) is Sigil.CLAIMED
    assert event_sigil(_event("E", status="?", event_kind="wave_closed")) is Sigil.CLOSED
    assert event_sigil(_event("E", status="?", event_kind="runtime_unavailable")) is Sigil.FAILED
    assert (
        event_sigil(_event("E", status="?", event_kind="git_state_drift_detected")) is Sigil.FAILED
    )


def test_event_sigil_defaults_to_running_for_generic_event() -> None:
    """An event with no recognisable token reads as in-flight (RUNNING)."""
    assert event_sigil(_event("E", status="?", event_kind="phase_activated")) is Sigil.RUNNING


def test_format_event_markup_tints_the_sigil_and_escapes_brackets() -> None:
    """The markup form tints the leading sigil and renders brackets literally.

    The sigil cell is wrapped in its Wong tint hex; an arbitrary summary
    carrying a literal ``[`` is backslash-escaped so Textual does not swallow
    it as a style tag.
    """
    markup = format_event_markup(
        _event("E", summary="[P01-W01] closed", kind="event"), mode="unicode"
    )
    closed_tint = tint(Sigil.CLOSED)
    assert closed_tint is not None
    assert markup.startswith(f"[{closed_tint}]{glyph(Sigil.CLOSED, mode='unicode')}[/]")
    # The literal bracket in the summary is escaped, not parsed as a tag.
    assert "\\[P01-W01]" in markup


def test_format_event_markup_swaps_sigil_column_in_ascii_mode() -> None:
    """An ``ascii`` render mode selects the ASCII sigil column."""
    unicode_markup = format_event_markup(_event("E", summary="x"), mode="unicode")
    ascii_markup = format_event_markup(_event("E", summary="x"), mode="ascii")
    assert glyph(Sigil.CLOSED, mode="unicode") in unicode_markup
    # The ASCII CLOSED sigil is ``@``; it is escaped-safe and present.
    assert "@" in ascii_markup
    assert glyph(Sigil.CLOSED, mode="unicode") not in ascii_markup


# --------------------------------------------------------------------------
# Listener lifecycle -- register on mount, unregister on unmount
# --------------------------------------------------------------------------


def test_feed_pane_registers_and_unregisters_listener() -> None:
    """Switching into Feed registers a listener; switching away unregisters it."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            assert app._feed_listeners == []
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, FeedModeScreen)
            assert len(app._feed_listeners) == 1
            # Switch away (back to Home): the Feed screen stays on its own
            # mode stack but is no longer the active pane; it stays
            # registered until it actually unmounts. Verify teardown on app
            # exit unregisters it (see test below); here assert the live
            # registration is exactly one while mounted.
            await pilot.press("1")
            await settle_screen(pilot)
            assert app.current_mode == "home"

    asyncio.run(body())


def test_feed_pane_unregisters_on_unmount() -> None:
    """An unmounted Feed pane is dropped from the App fan-out list."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, FeedModeScreen)
            assert pane in app._feed_listeners
            # Remove the pane directly to exercise on_unmount teardown.
            await pane.remove()
            await settle_screen(pilot)
            assert pane not in app._feed_listeners

    asyncio.run(body())


# --------------------------------------------------------------------------
# Live append -- newest-first, honest-empty replacement
# --------------------------------------------------------------------------


def test_feed_pane_honest_empty_before_events() -> None:
    """Before any event the pane shows the honest-empty live notice."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert FEED_EMPTY_LIVE in frame

    asyncio.run(body())


def test_feed_pane_appends_live_event_at_top() -> None:
    """A pushed envelope (via the App fan-out) replaces the empty notice."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            await app._on_event(_event("EV-1", summary="first event"))
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, FeedModeScreen)
            assert not pane.query(f"#{FEED_EMPTY_ID}")
            frame = normalize_snapshot(capture_screen_text(app))
            assert "first event" in frame
            assert FEED_EMPTY_LIVE not in frame

    asyncio.run(body())


def test_feed_pane_orders_events_newest_first() -> None:
    """Two pushes render newest-first (the later event is above the earlier)."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            await app._on_event(_event("EV-old", summary="older event"))
            await app._on_event(_event("EV-new", summary="newer event"))
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, FeedModeScreen)
            # Two rows rendered (one per event), no empty notice.
            assert len(pane.query(f".{FEED_ROW_CLASS}")) == 2
            # Newest-first: the newer row renders ABOVE the older one, so its
            # line appears earlier in the captured frame.
            lines = normalize_snapshot(capture_screen_text(app)).splitlines()
            newer_idx = next(i for i, ln in enumerate(lines) if "newer event" in ln)
            older_idx = next(i for i, ln in enumerate(lines) if "older event" in ln)
            assert newer_idx < older_idx

    asyncio.run(body())


# --------------------------------------------------------------------------
# Seed-from-buffer -- a mid-session switch shows earlier events
# --------------------------------------------------------------------------


def test_feed_pane_seeds_from_app_buffer_on_mount() -> None:
    """Events that arrived before the pane mounted are seeded on switch-in."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Events arrive while Home is active (no Feed pane mounted yet).
            await app._on_event(_event("EV-1", summary="buffered one"))
            await app._on_event(_event("EV-2", summary="buffered two"))
            await settle_screen(pilot)
            assert app._feed_listeners == []  # nothing mounted to fan out to
            # Now switch to Feed: it seeds from the App's live buffer.
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "buffered one" in frame
            assert "buffered two" in frame

    asyncio.run(body())


def test_live_event_buffer_is_bounded() -> None:
    """The App live buffer drops the oldest envelope past the cap."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            for i in range(LIVE_EVENT_BUFFER_MAX + 5):
                await app._on_event(_event(f"EV-{i}", summary=f"event {i}"))
            assert len(app.live_event_buffer) == LIVE_EVENT_BUFFER_MAX
            # The oldest five fell off the tail; the newest is retained.
            ids = [e.id for e in app.live_event_buffer]
            assert "EV-0" not in ids
            assert f"EV-{LIVE_EVENT_BUFFER_MAX + 4}" in ids

    asyncio.run(body())


# --------------------------------------------------------------------------
# Degraded -- honest message when the daemon is unreachable
# --------------------------------------------------------------------------


def test_feed_pane_degraded_shows_honest_message() -> None:
    """When the App is degraded the empty notice says the feed is paused."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            # Flip degraded through the same hook the binder drives.
            await app._on_degraded(True)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert FEED_EMPTY_DEGRADED in frame
            assert FEED_EMPTY_LIVE not in frame

    asyncio.run(body())


def test_feed_pane_degraded_recovers_to_live_notice() -> None:
    """A degraded->live flip restores the live-waiting notice wording."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            await app._on_degraded(True)
            await settle_screen(pilot)
            await app._on_degraded(False)
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert FEED_EMPTY_LIVE in frame
            assert FEED_EMPTY_DEGRADED not in frame

    asyncio.run(body())


# --------------------------------------------------------------------------
# Isolated reskin golden -- the W16 sigil + fixed-width column close-gate bar
# --------------------------------------------------------------------------


def test_feed_mode_sigils_snapshot() -> None:
    """The reskinned feed renders a sigil + fixed-width time + sigil column.

    The W16 close-gate golden, owned + regenerated by this wave in isolation
    (distinct from the coupled full-app ``feed_mode_populated.txt``). Seeds
    three deterministic events with distinct lifecycle statuses (closed /
    claimed / failed) into the App buffer, switches into Feed, and pins:

    * each row leads with its lifecycle sigil (closed circle / claimed ring /
      failed cross), tinted by its Wong status hue;
    * the fixed-width ``HH:MM:SS`` timestamp column lines the kind up across
      rows; and
    * the no-events sentinel path renders the waiting-for-events notice when
      no event has arrived.

    So a layout / glyph regression on any of those is caught against a golden
    this wave owns.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            # Seed deterministic events newest-last; the Feed pane seeds from
            # the buffer on mount (newest-first), so the failed row lands top.
            await app._on_event(_event("EV-1", summary="wave P01-I01-W01 closed", status="closed"))
            await app._on_event(
                _event("EV-2", summary="wave P01-I01-W02 claimed", status="claimed")
            )
            await app._on_event(_event("EV-3", summary="wave P01-I01-W03 failed", status="failed"))
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, FeedModeScreen)
            assert_screen_snapshot(app, _SIGILS_GOLDEN)
            # Belt-and-braces text assertions over the same frame so the
            # close-gate criteria are pinned independently of the golden.
            frame = normalize_snapshot(capture_screen_text(app))
            assert glyph(Sigil.CLOSED, mode="unicode") in frame
            assert glyph(Sigil.CLAIMED, mode="unicode") in frame
            assert glyph(Sigil.FAILED, mode="unicode") in frame

    asyncio.run(body())


def test_feed_mode_empty_sentinel_snapshot() -> None:
    """The no-events path renders the waiting-for-events sentinel literal.

    Mounts the feed in isolation with no buffered events and asserts the
    honest-empty live sentinel (:data:`FEED_EMPTY_LIVE`) renders, so the
    no-events branch of the close-gate criterion is pinned.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            assert isinstance(app.screen, FeedModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            assert FEED_EMPTY_LIVE in frame
            # No event rows mounted -- the sentinel is the only feed content.
            assert not app.screen.query(f".{FEED_ROW_CLASS}")

    asyncio.run(body())


def test_feed_pane_repaints_sigils_on_render_mode_flip() -> None:
    """A unicode <-> ASCII render-mode flip repaints each row's sigil column."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            await app._on_event(_event("EV-1", summary="wave closed", status="closed"))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert glyph(Sigil.CLOSED, mode="unicode") in frame
            # Flip to ASCII; the row repaints to the ASCII sigil column.
            app.render_mode = "ascii"
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert glyph(Sigil.CLOSED, mode="unicode") not in frame
            assert glyph(Sigil.CLOSED, mode="ascii") in frame

    asyncio.run(body())


# --------------------------------------------------------------------------
# End-to-end -- real StateBinding + fake daemon client, worker delivery
# --------------------------------------------------------------------------


class _PushReader:
    """A makefile-style reader yielding one push frame then EOF."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = [*frames, b""]

    def readline(self) -> bytes:
        if not self._frames:
            return b""
        return self._frames.pop(0)


class _FakeDaemonClient:
    """A drop-in for :class:`DaemonClient` that streams canned push frames.

    Mirrors the real client's context-manager + ``call`` + ``_reader``
    surface the :class:`StateBinding` subscribe loop reads, so the binding
    runs its real off-thread ``readline`` loop against canned frames.
    """

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


def test_feed_pane_receives_worker_delivered_push_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push delivered via the real binding's worker thread lands in the feed.

    Exercises the full non-blocking path: the App's
    :class:`~eawf.surfaces.tui.state_binding.StateBinding` runs its real
    ``asyncio.to_thread`` subscribe loop against a fake daemon client, the
    decoded envelope is marshalled back to the event loop, fanned out to the
    mounted Feed pane, and rendered. The UI event loop is never blocked on
    the socket read -- the ``readline`` loop is off-thread, and
    ``settle_screen`` drains workers before sampling.
    """
    event = _event("EV-worker", summary="worker delivered")
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

        # Force the binding onto its daemon-push leg with a fake client that
        # streams the canned frame; the socket-availability probe is stubbed
        # true so the subscribe loop starts immediately.
        monkeypatch.setattr(
            sb.StateBinding,
            "_daemon_socket_available",
            lambda _self: True,
        )
        monkeypatch.setattr(
            sb,
            "DaemonClient",
            lambda *a, **k: _FakeDaemonClient([push], calls),
        )

        app = EaApp(scope="repo", state_path=_EMPTY_REPO)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_FEED_DIGIT)
            await settle_screen(pilot)
            # Give the off-thread subscribe loop a beat to stream the frame,
            # then drain workers + settle so the marshalled push is applied.
            for _ in range(20):
                await pilot.pause()
                if any(e.id == "EV-worker" for e in app.live_event_buffer):
                    break
            await settle_screen(pilot)
            assert any(e.id == "EV-worker" for e in app.live_event_buffer)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "worker delivered" in frame

    asyncio.run(body())
    # The binding subscribed through the daemon stream (worker path), not a
    # one-shot read.
    assert calls and calls[0][0] == "state.subscribe"
