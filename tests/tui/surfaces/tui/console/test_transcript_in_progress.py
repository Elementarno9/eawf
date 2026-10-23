"""The in-progress row at the foot of a live Run's transcript.

A quiet Run is read by asking what it is doing now: thinking, waiting on a tool, or not
yet admitted. While the Run's stored status says it is live, the last row under the
blocks names that, greyed, with how long it has gone on; when the awaited block lands it
is drawn as a normal row with its stats and the in-progress row starts timing again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from eawf.kernel.projection.transcript import TranscriptReadModel
from eawf.kernel.runtime.events import (
    CommandPayload,
    MessageSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.surfaces.tui.console.frame import Receded, View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb
from tests.tui.surfaces.tui.console import test_transcript_durations as td

#: The Run every probe document holds, keyed as the projection keys it.
RUN_KEY: Final = "RUN-00000010"


def _document(status: str) -> dict[str, Any]:
    """Return the probe document with its one Run in ``status``."""
    return {"run": {RUN_KEY: {**tb.DOCUMENT["run"][RUN_KEY], "status": status}}}


def _model(events: Any = (), *, status: str = "RUNNING") -> TranscriptReadModel:
    return tb._view(events, document=_document(status))


def _rows(
    model: TranscriptReadModel,
    *,
    now: datetime | None,
    held: bool = False,
    h: int = 30,
    session: Session | None = None,
) -> list[str]:
    """Return the frame's rows as drawn, following the tail unless ``session`` says otherwise."""
    if session is None:
        session = Session()
        session.follow = True
    session.route = tb.TRANSCRIPT_ROUTE
    view = View(
        session=session, fixture=tb._fixture(), w=120, h=h, projection=model, now=now, held=held
    )
    return render_route(view)


def _foot(rows: list[str]) -> str:
    """Return the last row of the block section: the one above the closing rule."""
    closing = max(i for i, row in enumerate(rows) if row.startswith(" OUTCOME"))
    return rows[closing - 2]


def _said(sequence: int, at: int) -> RunEventRecord:
    return td._said(sequence, at)


# ---- running: the row names the tool in flight, else the model turn -------------------


def test_running_row_names_the_tool_in_flight_and_its_elapsed() -> None:
    events = (_said(1, 0), td._provider(2, td.FIRST, "requested", 10))
    foot = _foot(_rows(_model(events), now=tb.AT + timedelta(seconds=25)))
    assert isinstance(foot, Receded)
    assert foot.rstrip().endswith("waiting on tool bash · 15s")


def test_running_row_names_a_gateway_call_requested_and_not_yet_accepted() -> None:
    """A requested-only gateway call is not in flight, yet the Run is waiting on it."""
    events = (_said(1, 0), td._gateway(2, td.FIRST, "requested", 4))
    foot = _foot(_rows(_model(events), now=tb.AT + timedelta(seconds=6)))
    assert foot.rstrip().endswith("waiting on tool repo_read · 2s")


def test_running_row_says_model_turn_timed_from_the_latest_block() -> None:
    events = (_said(1, 0), _said(2, 30))
    foot = _foot(_rows(_model(events), now=tb.AT + timedelta(seconds=158)))
    assert isinstance(foot, Receded)
    assert foot.rstrip().endswith("model turn · 2m 08s")


def test_running_row_is_a_model_turn_beside_a_command_in_the_background() -> None:
    """Work detached into the background does not hold the Run, so it is not awaited."""
    detached = tb._command(2, execution="background")
    assert isinstance(detached.payload, CommandPayload)
    foot = _foot(_rows(_model((_said(1, 0), detached)), now=tb.AT + timedelta(seconds=5)))
    assert foot.rstrip().endswith("model turn · 3s")


def test_running_row_on_a_run_with_no_block_states_the_turn_untimed() -> None:
    """Empty: nothing has landed to time from, so the row names the turn alone."""
    rows = _rows(_model(), now=tb.AT)
    foot = _foot(rows)
    assert isinstance(foot, Receded)
    assert foot.rstrip().endswith("model turn")
    assert any(tb.transcript.NO_BLOCK in row for row in rows)


def test_running_row_elapsed_steps_from_seconds_to_minutes() -> None:
    """Off by one: 59 seconds read ``59s`` and the next second ``1m 00s``."""
    model = _model((_said(1, 0),))
    assert _foot(_rows(model, now=tb.AT + timedelta(seconds=59))).rstrip().endswith("59s")
    assert _foot(_rows(model, now=tb.AT + timedelta(seconds=60))).rstrip().endswith("1m 00s")


# ---- queued and ended: admission, then nothing once the Run is over -------------------


def test_queued_and_ended_queued_row_waits_on_admission() -> None:
    foot = _foot(_rows(_model((_said(1, 0),), status="QUEUED"), now=tb.AT + timedelta(seconds=9)))
    assert isinstance(foot, Receded)
    assert foot.rstrip().endswith("waiting on admission · 9s")


def test_queued_and_ended_queued_row_with_no_block_is_untimed() -> None:
    foot = _foot(_rows(_model(status="QUEUED"), now=tb.AT))
    assert foot.rstrip().endswith("waiting on admission")


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "CANCELLED"])
def test_queued_and_ended_ended_run_draws_no_row(status: str) -> None:
    rows = _rows(_model((_said(1, 0),), status=status), now=tb.AT + timedelta(seconds=9))
    assert not any(isinstance(row, Receded) for row in rows)
    assert not any("model turn" in row or "waiting on" in row for row in rows)


def test_queued_and_ended_no_held_run_draws_no_row() -> None:
    """Missing key: with no Run row there is no stored status to read, so nothing is drawn."""
    model = tb._view((tb._event(1),), document={"run": {}})
    rows = _rows(model, now=tb.AT + timedelta(seconds=9))
    assert not any(isinstance(row, Receded) for row in rows)


def test_queued_and_ended_row_never_takes_the_cursor() -> None:
    session = Session()
    session.follow = True
    model = _model((_said(1, 0), _said(2, 1)))
    rows = _rows(model, now=tb.AT + timedelta(seconds=5), session=session)
    assert session.tr_sel == 1
    assert session.nav_rows == 2
    foot = _foot(rows)
    assert not foot.startswith("▸")
    assert [row[:9] for row in rows if row.startswith("▸")] == ["▸12:00:01"]


def test_queued_and_ended_following_window_keeps_the_row_on_screen() -> None:
    """Max length: a Run longer than the section still ends on its in-progress row."""
    events = tuple(_said(n, n) for n in range(1, 41))
    rows = _rows(_model(events), now=tb.AT + timedelta(seconds=43), h=20)
    foot = _foot(rows)
    assert isinstance(foot, Receded)
    assert "model turn · 3s" in foot
    assert any("above" in row for row in rows)


def test_queued_and_ended_opened_long_block_keeps_the_caret_over_the_row() -> None:
    """A block opened past the section's height keeps its head on screen; the row waits below."""
    words = " ".join(f"word{n}" for n in range(70))
    long = td._line(
        2,
        RunEventKind.MESSAGE_SUMMARIZED,
        MessageSummaryPayload(message_role="assistant", summary=words),
        2,
    )
    session = Session()
    session.follow = True
    session.tr_fold = {1: True}
    rows = _rows(
        _model((_said(1, 0), long)), now=tb.AT + timedelta(seconds=5), session=session, h=12
    )
    assert any(row.startswith("▸12:00:02") for row in rows)
    assert any("below" in row for row in rows)
    assert not any(isinstance(row, Receded) for row in rows)


# ---- completes into a row: the block lands with its stats, the timer restarts ---------


def test_completes_into_row_with_its_duration_and_restarts_the_timer() -> None:
    asked = (_said(1, 0), td._provider(2, td.FIRST, "requested", 10))
    before = _rows(_model(asked), now=tb.AT + timedelta(seconds=13))
    assert _foot(before).rstrip().endswith("waiting on tool bash · 3s")
    answered = (*asked, td._provider(3, td.FIRST, "result", 14))
    after = _rows(_model(answered), now=tb.AT + timedelta(seconds=20))
    call = next(row for row in after if "bash · " in row)
    assert not isinstance(call, Receded)
    assert call.rstrip().endswith("4s")
    assert _foot(after).rstrip().endswith("model turn · 6s")


def test_completes_into_row_frozen_under_a_held_clock() -> None:
    """A held clock reads no wall time, so the row is timed to the authored tail."""
    model = _model((_said(1, 0), _said(2, 30)))
    early = _foot(_rows(model, now=tb.AT + timedelta(seconds=40), held=True))
    late = _foot(_rows(model, now=tb.AT + timedelta(seconds=400), held=True))
    assert early == late
    assert early.rstrip().endswith("model turn · 0s")


@pytest.mark.parametrize("status", ["SUSPENDED", "DREAMING"])
def test_queued_and_ended_other_status_draws_no_row(status: str) -> None:
    """Out of range: only a queued or running Run is doing something to time."""
    rows = _rows(_model((_said(1, 0),), status=status), now=tb.AT + timedelta(seconds=9))
    assert not any(isinstance(row, Receded) for row in rows)
