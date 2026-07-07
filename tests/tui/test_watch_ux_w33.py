"""P30-I21-W33 (G5+G6+G10): watch-pane liveness heartbeat + layout.

The watch surface showed no liveness heartbeat or elapsed-versus-expected and
squeezed the stream pane so long JSON read as cut off. This wave adds a
``thinking · <elapsed>/~<expected> · <turns> turns · pid <pid>`` heartbeat
(G5/G6, effort-aware from the wave's bucket) and gives the stream pane the
majority of the body height with a fuller backfill (G10). These tests pin the
pure formatting + the layout constants.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import AgentSessionStatus, EffortBucket, WaveStatus
from eawf.surfaces.tui.modes.agent_watch import (
    _OUTPUT_BACKFILL_LIMIT,
    WatchTarget,
    _expected_minutes,
    _format_duration,
    render_liveness_line,
    render_watch_header,
)

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _target(
    *,
    wave_status: WaveStatus | None = WaveStatus.IN_PROGRESS,
    effort_bucket: EffortBucket | None = EffortBucket.L,
    subprocess_pid: int | None = 4242,
    started_at: datetime | None = _T0,
) -> WatchTarget:
    return WatchTarget(
        session_id="S-1",
        wave_id="P01-I01-W01",
        runtime="claude",
        status=AgentSessionStatus.ACTIVE,
        attempt=1,
        wave_status=wave_status,
        subprocess_pid=subprocess_pid,
        started_at=started_at,
        effort_bucket=effort_bucket,
    )


def test_format_duration_units() -> None:
    """Duration reads seconds-precise, minute-precise, then hour+minute."""
    assert _format_duration(45) == "45s"
    assert _format_duration(134) == "2m14s"
    assert _format_duration(3600) == "1h00m"
    assert _format_duration(3661) == "1h01m"
    assert _format_duration(-5) == "0s"


def test_expected_minutes_from_bucket() -> None:
    """Expected wall-clock is the bucket centroid EU in minutes (L = 60)."""
    assert _expected_minutes(EffortBucket.L) == 60.0
    assert _expected_minutes(EffortBucket.M) == 30.0
    assert _expected_minutes(None) is None


def test_liveness_line_shows_thinking_elapsed_expected_turns_pid() -> None:
    """The heartbeat carries thinking, elapsed/~expected, turns, and pid."""
    now = _T0.replace(minute=2, second=14)  # 2m14s after start
    line = render_liveness_line(_target(), turns=6, now=now)
    assert "thinking" in line
    assert "2m14s/~1h00m" in line
    assert "6 turns" in line
    assert "pid 4242" in line


def test_liveness_line_without_bucket_omits_expected() -> None:
    """With no effort bucket the heartbeat shows bare elapsed, no /~expected."""
    now = _T0.replace(minute=1, second=0)
    line = render_liveness_line(_target(effort_bucket=None), turns=1, now=now)
    assert "1m00s" in line
    assert "/~" not in line


def test_liveness_line_empty_for_terminal_wave() -> None:
    """A terminal (replay) wave has no live heartbeat."""
    assert render_liveness_line(_target(wave_status=WaveStatus.CLOSED), turns=3, now=_T0) == ""


def test_watch_header_appends_liveness_when_now_supplied() -> None:
    """render_watch_header appends the heartbeat only when now is supplied."""
    now = _T0.replace(minute=0, second=30)
    with_now = render_watch_header(_target(), turns=2, now=now)
    without_now = render_watch_header(_target())
    assert "thinking" in with_now
    assert "\n" in with_now
    # The default single-line header stays a pure function of the target.
    assert "thinking" not in without_now


def test_output_backfill_limit_raised_for_g10() -> None:
    """The backfill cap is relaxed so more history seeds the tail (G10)."""
    assert _OUTPUT_BACKFILL_LIMIT >= 2000


def test_watch_output_pane_is_taller_than_events() -> None:
    """The stream pane gets more body height than the events pane (G10)."""
    from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen

    css = AgentWatchModeScreen.DEFAULT_CSS
    assert "#watch-output {\n        height: 3fr;" in css
    assert "#watch-list {\n        height: 1fr;" in css
