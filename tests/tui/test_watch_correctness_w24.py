"""W24: watch-mode trust + selected-row contrast defect cluster.

Three defects the FA3/FA4 watch surface shipped are pinned here:

* **Contrast** -- a selected lane / picker row painted the SELECTION_TINT green
  band but kept its per-span ``$muted`` / ``$accent`` content-markup colours,
  which fail contrast on that green (Rich content-markup colours override the
  widget CSS ``color``). The selected row is now rendered PLAIN so the
  ``.-selected`` ``color: $text`` paints it readably; the unselected row keeps
  its semantic colours on the dark background where they read fine.
* **Target trust** -- ``pick_watch_target`` counted a session whose WAVE was
  already terminal (failed / closed / abandoned) as "active" whenever its
  session record still read ACTIVE, and ``render_watch_header`` labelled the
  header from that stale session status. Both now cross-check the wave: a
  failed wave is never in the active pool and is never labelled ``active``.
* **Replay honesty + Esc** -- the output-tail replay of a terminal-not-closed
  wave echoed the agent's self-claimed pass unqualified; it is now framed with
  a banner naming the wave's real terminal status (the raw replay kept below).
  ``Esc`` out of a finished-session zoom relaxes the picker-return gate to one
  row (so a single finished session returns to the browsable picker) and only
  falls back to the feed when there is truly nothing to browse.

Pilot bodies drain workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen`
before asserting, per the project Pilot-worker determinism rule.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    Project,
    State,
    Wave,
)
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.agent_watch import (
    WATCH_HEADER_ID,
    WATCH_REPLAY_VERDICT_BANNER,
    AgentWatchModeScreen,
    LaneGrid,
    LaneGridRow,
    LaneState,
    SessionPicker,
    SessionPickerRow,
    WatchTarget,
    frame_replay_lines,
    pick_watch_target,
    render_lane_row,
    render_picker_row,
    render_watch_header,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_WATCH_DIGIT = "8"
_WAVE = "P01-I01-W01"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _session(
    sid: str = "S-1",
    *,
    scope_id: str = _WAVE,
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    runtime: str = "claude",
    started_at: datetime = _T0,
) -> AgentSession:
    """Build an agent-session row for the watch-target picker."""
    return AgentSession(
        id=sid,
        role=role,
        runtime=runtime,
        scope_id=scope_id,
        status=status,
        started_at=started_at,
    )


def _wave(wave_id: str = _WAVE, *, status: WaveStatus) -> Wave:
    """Build a minimal wave row carrying only the lifecycle *status*."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return Wave(
        id=wave_id,
        iter_id=iter_id,
        title=f"Wave {wave_id}",
        status=status,
        opened_at=_T0,
    )


def _state(
    *,
    sessions: dict[str, AgentSession] | None = None,
    waves: dict[str, Wave] | None = None,
) -> State:
    """Build a minimal repo state, optionally with agent sessions + waves."""
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


def _chunk_row(wave_id: str, *, seq: int, lines: str) -> str:
    """One ``agent.output.chunk`` event-store JSONL row for *wave_id*."""
    return json.dumps(
        {
            "kind": "event",
            "scope_id": wave_id,
            "payload": {
                "event_type": "agent.output.chunk",
                "wave_id": wave_id,
                "seq": seq,
                "lines": lines,
            },
        }
    )


def _lane_row(*, state: LaneState = LaneState.RUNNING) -> LaneGridRow:
    """Build an FA3 lane-grid display row."""
    return LaneGridRow(
        wave_id=_WAVE,
        vendor="claude",
        elapsed_label="3m",
        spend_label="12k tok $0.40",
        tier_badge="MECH",
        sandbox_label="open",
        state=state,
    )


def _picker_row(*, status: AgentSessionStatus = AgentSessionStatus.CLOSED) -> SessionPickerRow:
    """Build a session-picker display row."""
    return SessionPickerRow(
        session_id="S-1",
        wave_id=_WAVE,
        runtime="claude",
        status=status,
        started_label="12:00",
    )


# --------------------------------------------------------------------------
# Criterion 1 -- selected-row contrast (lane grid + session picker)
# --------------------------------------------------------------------------


def test_render_lane_row_selected_drops_colour_markup() -> None:
    """A selected lane row is PLAIN so the .-selected color:$text paints it."""
    plain = render_lane_row(_lane_row(), selected=True, mode="unicode")

    assert "[$muted]" not in plain
    assert "[$accent]" not in plain
    # The cells are still all present, just without per-span colour markup.
    assert _WAVE in plain
    assert "claude" in plain
    assert "MECH" in plain


def test_render_lane_row_unselected_keeps_semantic_colours() -> None:
    """The non-selected lane row keeps its semantic colours on the dark bg."""
    coloured = render_lane_row(_lane_row(), selected=False, mode="unicode")

    assert "[$muted]" in coloured
    assert "[$accent]" in coloured
    assert _WAVE in coloured


def test_lane_grid_selected_css_sets_readable_foreground() -> None:
    """The .watch-lane-row.-selected rule adds color:$text on the tint band."""
    css = LaneGrid.DEFAULT_CSS

    assert ".watch-lane-row.-selected" in css
    # The SELECTION_TINT band plus an explicit bright foreground.
    assert "background: #0c5a44;" in css
    assert "color: $text;" in css


def test_render_picker_row_selected_drops_colour_markup() -> None:
    """A selected picker row is PLAIN so the .-selected color:$text paints it."""
    plain = render_picker_row(_picker_row(), selected=True, mode="unicode")

    assert "[$muted]" not in plain
    assert "[$accent]" not in plain
    assert "[$text]" not in plain
    assert _WAVE in plain
    assert "closed" in plain


def test_render_picker_row_unselected_keeps_semantic_colours() -> None:
    """The non-selected picker row keeps its semantic colours."""
    coloured = render_picker_row(_picker_row(), selected=False, mode="unicode")

    assert "[$muted]" in coloured
    assert "[$text]" in coloured
    assert _WAVE in coloured


def test_session_picker_selected_css_sets_readable_foreground() -> None:
    """The .watch-picker-row.-selected rule adds color:$text on the tint band."""
    css = SessionPicker.DEFAULT_CSS

    assert ".watch-picker-row.-selected" in css
    assert "background: #0c5a44;" in css
    assert "color: $text;" in css


# --------------------------------------------------------------------------
# Criterion 2 -- target pick + header trust the wave, not a stale session row
# --------------------------------------------------------------------------


def test_pick_watch_target_excludes_terminal_wave_from_active_pool() -> None:
    """An ACTIVE session on a FAILED wave loses to a live-wave ACTIVE session.

    The failed wave's session still reads ACTIVE (the spawn dropped off the live
    stream without closing its record), and it is the MOST recent -- so without
    the wave cross-check it would win the default pick. The live wave's session
    must be preferred instead.
    """
    live = _session(
        "S-live", scope_id="P01-I01-W01", started_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    )
    stale = _session(
        "S-stale", scope_id="P01-I01-W02", started_at=datetime(2026, 5, 27, 13, 0, tzinfo=UTC)
    )
    state = _state(
        sessions={"S-live": live, "S-stale": stale},
        waves={
            "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.IN_PROGRESS),
            "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.FAILED),
        },
    )

    target = pick_watch_target(state)

    assert target is not None
    assert target.wave_id == "P01-I01-W01"  # the live wave, not the newer failed one
    assert target.wave_status is WaveStatus.IN_PROGRESS


def test_pick_watch_target_falls_back_when_only_terminal_wave_sessions() -> None:
    """With only terminal-wave sessions the pick still returns one (replay)."""
    stale = _session("S-stale", scope_id="P01-I01-W02", status=AgentSessionStatus.ACTIVE)
    state = _state(
        sessions={"S-stale": stale},
        waves={"P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.FAILED)},
    )

    target = pick_watch_target(state)

    assert target is not None
    assert target.wave_id == "P01-I01-W02"
    assert target.wave_status is WaveStatus.FAILED


def test_render_watch_header_labels_from_wave_when_terminal() -> None:
    """A failed wave with a still-ACTIVE session record is labelled 'failed'."""
    target = WatchTarget(
        session_id="S-1",
        wave_id=_WAVE,
        runtime="claude",
        status=AgentSessionStatus.ACTIVE,
        attempt=1,
        wave_status=WaveStatus.FAILED,
    )

    header = render_watch_header(target, mode="ascii")

    assert "failed" in header
    assert "active" not in header


def test_render_watch_header_labels_from_session_when_wave_active() -> None:
    """A non-terminal wave keeps the session lifecycle status word."""
    target = WatchTarget(
        session_id="S-1",
        wave_id=_WAVE,
        runtime="claude",
        status=AgentSessionStatus.ACTIVE,
        attempt=1,
        wave_status=WaveStatus.IN_PROGRESS,
    )

    header = render_watch_header(target, mode="ascii")

    assert "active" in header


def test_render_watch_header_terminal_wave_sigil_is_not_running() -> None:
    """A failed wave draws the failed sigil, not the RUNNING diamond."""
    running = render_watch_header(
        WatchTarget(
            session_id="S-1",
            wave_id=_WAVE,
            runtime="claude",
            status=AgentSessionStatus.ACTIVE,
            attempt=1,
            wave_status=WaveStatus.IN_PROGRESS,
        ),
        mode="unicode",
    )
    failed = render_watch_header(
        WatchTarget(
            session_id="S-1",
            wave_id=_WAVE,
            runtime="claude",
            status=AgentSessionStatus.ACTIVE,
            attempt=1,
            wave_status=WaveStatus.FAILED,
        ),
        mode="unicode",
    )

    # The two headers lead with different lifecycle sigils -- the failed wave
    # does not borrow the still-ACTIVE session's RUNNING mark.
    assert running.split()[0] != failed.split()[0]


# --------------------------------------------------------------------------
# Criterion 3a -- terminal-not-closed replay is framed with the real verdict
# --------------------------------------------------------------------------


def test_frame_replay_lines_failed_wave_prepends_banner() -> None:
    """A failed wave's replay gets the verdict banner prepended."""
    framed = frame_replay_lines(["agent: all criteria PASS"], WaveStatus.FAILED)

    assert framed[0] == WATCH_REPLAY_VERDICT_BANNER.format(status="failed")
    # The raw self-claim is kept BELOW the banner -- not censored.
    assert framed[1] == "agent: all criteria PASS"


def test_frame_replay_lines_abandoned_wave_prepends_banner() -> None:
    """An abandoned wave's replay gets the verdict banner prepended."""
    framed = frame_replay_lines(["line"], WaveStatus.ABANDONED)

    assert framed[0] == WATCH_REPLAY_VERDICT_BANNER.format(status="abandoned")
    assert framed[1:] == ["line"]


def test_frame_replay_lines_closed_wave_is_unframed() -> None:
    """A cleanly-closed wave carries a recorded verdict, so no banner."""
    assert frame_replay_lines(["line"], WaveStatus.CLOSED) == ["line"]


def test_frame_replay_lines_none_and_nonterminal_are_unframed() -> None:
    """An unknown or non-terminal wave replays unframed."""
    assert frame_replay_lines(["line"], None) == ["line"]
    assert frame_replay_lines(["line"], WaveStatus.IN_PROGRESS) == ["line"]


def test_agent_watch_terminal_replay_shows_verdict_banner(tmp_path: Path) -> None:
    """The mounted zoom frames a failed wave's tail with the verdict banner.

    A single session on a FAILED wave auto-zooms; the on-mount store-sync reads
    the persisted chunk and prepends the banner naming the wave's real terminal
    status, keeping the agent's self-claimed pass visible below it.
    """
    state = _state(
        sessions={"S-1": _session("S-1", status=AgentSessionStatus.ACTIVE)},
        waves={_WAVE: _wave(_WAVE, status=WaveStatus.FAILED)},
    )
    state_path = _write_state(tmp_path, state)
    store = state_path.parent / "store"
    store.mkdir(parents=True, exist_ok=True)
    (store / "event.jsonl").write_text(
        _chunk_row(_WAVE, seq=0, lines="agent: all criteria PASS") + "\n",
        encoding="utf-8",
    )

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            frame = normalize_snapshot(capture_screen_text(app))
            # The wave's real terminal status frames the replay ...
            assert "wave failed" in frame
            # ... and the agent's raw self-claim is still shown below it.
            assert "all criteria PASS" in frame

    asyncio.run(body())


# --------------------------------------------------------------------------
# Criterion 3b -- Esc returns to the picker (>=1 row), else falls back to feed
# --------------------------------------------------------------------------


def test_leave_zoom_single_finished_session_returns_to_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Esc out of a single finished-session zoom steps back to the picker.

    A lone finished session auto-zooms on mount (a one-row picker is pointless),
    but Esc relaxes the picker-return gate to one row so the operator lands on a
    browsable picker rather than being thrown to another mode.
    """
    state = _state(sessions={"S-1": _session("S-1", status=AgentSessionStatus.CLOSED)})
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            # A single finished session auto-zooms (no picker mounted on mount).
            assert pane.query(f"#{WATCH_HEADER_ID}"), "expected the auto-zoom on mount"
            assert not pane.query(SessionPicker)
            await pilot.press("escape")
            await settle_screen(pilot)
            # Esc steps out to the picker; the mode is unchanged (not feed).
            assert pane.query(SessionPicker), "Esc should return to the picker"
            assert app.current_mode != "feed"

    asyncio.run(body())


def test_leave_zoom_no_sessions_falls_back_to_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing to browse, Esc falls back to the feed as the final path."""
    state = _state()  # no executor sessions at all
    state_path = _write_state(tmp_path, state)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        monkeypatch.setattr(EaApp, "_daemon_socket_available", lambda _self: False)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_WATCH_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AgentWatchModeScreen)
            await pilot.press("escape")
            await settle_screen(pilot)
            # No finished session to browse -> the feed is the honest fallback.
            assert app.current_mode == "feed"

    asyncio.run(body())
