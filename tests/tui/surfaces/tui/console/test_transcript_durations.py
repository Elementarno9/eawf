"""How long each transcript row took: a tool call to its result, a model turn to its reading.

A slow Run is read by asking where its time went. A folded tool call states the seconds
from its request line to its result line, and the first block after a usage reading
states the model turn that reading closed: from the latest tool result before it, or
from the Run's first line when no tool has returned yet. The row's right-hand note shows
both once the work is done; work still in flight keeps its running elapsed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from eawf.kernel.projection.transcript import build_transcript_blocks
from eawf.kernel.runtime.events import (
    MessageSummaryPayload,
    ProviderToolPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
)
from eawf.kernel.runtime.usage import UsagePayload
from eawf.surfaces.tui.console.frame import Breadth
from eawf.surfaces.tui.console.renderers import transcript
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb

#: The tool calls a stream makes, in the order it makes them.
FIRST: Final = "call-00000000000000a1"
SECOND: Final = "call-00000000000000b1"
THIRD: Final = "call-00000000000000c1"

#: A reading the stream closes a model turn with.
USAGE: Final = UsagePayload(
    usage_source="provider_transcript",
    is_cumulative=True,
    measurement_quality="measured",
    output_tokens=12,
)


def _line(sequence: int, kind: RunEventKind, payload: Any, at: int) -> RunEventRecord:
    """Return one line ``at`` seconds after noon, the probe's anchor."""
    return tb._event(sequence, event_kind=kind, payload=payload).model_copy(
        update={"recorded_at": tb.AT + timedelta(seconds=at)}
    )


def _said(sequence: int, at: int) -> RunEventRecord:
    payload = MessageSummaryPayload(message_role="assistant", summary=f"said {sequence}")
    return _line(sequence, RunEventKind.MESSAGE_SUMMARIZED, payload, at)


def _usage(sequence: int, at: int) -> RunEventRecord:
    return _line(sequence, RunEventKind.USAGE_OBSERVED, USAGE, at)


def _provider(sequence: int, call: str, phase: str, at: int) -> RunEventRecord:
    payload = ProviderToolPayload.model_validate(
        {
            "call_ref": call,
            "tool_name": "bash",
            "tool_class": "command",
            "phase": phase,
            "outcome": "ok" if phase == "result" else None,
        }
    )
    kind = RunEventKind.TOOL_RESULT if phase == "result" else RunEventKind.TOOL_REQUESTED
    return _line(sequence, kind, payload, at)


def _gateway(sequence: int, call: str, phase: str, at: int) -> RunEventRecord:
    receipt = {"result_ref": "receipt-00000000000000a1"} if phase == "result" else {}
    payload = ToolPayload.model_validate(
        {"call_ref": call, "tool_id": "repo_read", "phase": phase, **receipt}
    )
    kind = {
        "requested": RunEventKind.TOOL_REQUESTED,
        "accepted": RunEventKind.TOOL_ACCEPTED,
        "result": RunEventKind.TOOL_RESULT,
    }[phase]
    return _line(sequence, kind, payload, at)


# ---- a tool call's duration: request line to result line --------------------------------


def test_tool_call_duration_runs_from_the_request_line_to_the_result_line() -> None:
    """A gateway call is timed from its request, not from when the gateway accepted it."""
    events = (
        _gateway(1, FIRST, "requested", 2),
        _gateway(2, FIRST, "accepted", 3),
        _gateway(3, FIRST, "result", 9),
        _provider(4, SECOND, "requested", 10),
        _provider(5, SECOND, "result", 11),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [block.duration_seconds for block in blocks] == [7, 1]


def test_tool_call_duration_is_none_while_the_call_is_in_flight() -> None:
    events = (
        _gateway(1, FIRST, "requested", 0),
        _gateway(2, FIRST, "accepted", 4),
    )
    blocks, _ = build_transcript_blocks(events)
    assert blocks[0].in_flight is True
    assert blocks[0].duration_seconds is None


def test_tool_call_duration_is_none_for_a_call_with_no_result_in_an_ended_run() -> None:
    """Boundary: an ended Run holds nothing in flight; a call that never returned has no time."""
    blocks, _ = build_transcript_blocks((_provider(1, FIRST, "requested", 0),), ended=True)
    assert blocks[0].in_flight is False
    assert blocks[0].duration_seconds is None


def test_tool_call_duration_is_zero_for_a_result_in_the_same_second() -> None:
    """Off by one: a call that returned within its request's second states 0, not None."""
    events = (
        _provider(1, FIRST, "requested", 5),
        _provider(2, FIRST, "result", 5),
    )
    blocks, _ = build_transcript_blocks(events)
    assert blocks[0].duration_seconds == 0


def test_tool_call_duration_is_not_stated_on_any_other_block() -> None:
    blocks, _ = build_transcript_blocks((_said(1, 0), _usage(2, 3)))
    assert [block.duration_seconds for block in blocks] == [None]


def test_tool_call_duration_of_an_empty_stream_is_no_block() -> None:
    assert build_transcript_blocks(()) == ((), ())


# ---- a model turn's duration: latest tool result to the next usage reading --------------


def test_model_turn_duration_runs_from_the_latest_tool_result_to_the_reading() -> None:
    """A result at 12:00:00 and a reading at 12:00:38 give the block after the reading 38."""
    events = (
        _said(1, -5),
        _provider(2, FIRST, "requested", -1),
        _provider(3, FIRST, "result", 0),
        _usage(4, 38),
        _said(5, 40),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [block.turn_seconds for block in blocks] == [None, None, 38]


def test_model_turn_duration_runs_from_the_first_line_when_no_tool_returned() -> None:
    blocks, _ = build_transcript_blocks((_said(1, 0), _usage(2, 12), _said(3, 13)))
    assert [block.turn_seconds for block in blocks] == [None, 12]


def test_model_turn_duration_lands_on_the_folded_call_after_the_reading() -> None:
    """A call's result line is folded into its request's block, so it opens no block of its
    own; the turn lands on the call the model made after its reading."""
    events = (
        _said(1, 0),
        _usage(2, 20),
        _provider(3, FIRST, "requested", 21),
        _provider(4, FIRST, "result", 22),
        _usage(5, 50),
        _provider(6, SECOND, "requested", 51),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [(block.lane, block.turn_seconds) for block in blocks] == [
        ("message", None),
        ("tool", 20),
        ("tool", 28),
    ]


def test_model_turn_duration_after_a_trailing_reading_is_on_no_block() -> None:
    """Boundary: a reading the stream ends on has no block after it to carry its turn."""
    blocks, _ = build_transcript_blocks((_said(1, 0), _usage(2, 9)))
    assert [block.turn_seconds for block in blocks] == [None]


def test_model_turn_duration_of_a_lone_reading_is_on_no_block() -> None:
    """Single: the reading is the Run's first line, opens no block, and nothing follows it."""
    blocks, _ = build_transcript_blocks((_usage(1, 0),))
    assert blocks == ()


# ---- the right-hand note shows both, before any fold mark ------------------------------


def _durations_stream() -> tuple[RunEventRecord, ...]:
    """Return two finished calls after a 38-second turn each, then a call still going."""
    return (
        _said(1, 0),
        _usage(2, 38),
        _provider(3, FIRST, "requested", 40),
        _provider(4, FIRST, "result", 41),
        _usage(5, 79),
        _provider(6, SECOND, "requested", 80),
        _provider(7, SECOND, "result", 208),
        _provider(8, THIRD, "requested", 210),
    )


def test_note_shows_durations_of_a_turn_and_a_call_on_the_drawn_rows() -> None:
    rows = tb._frame(tb._view(_durations_stream())).split("\n")
    tools = [row for row in rows if "bash · " in row and "⋯ now" not in row]
    assert len(tools) == 3
    # the turn's tokens lead: the first reading's own, then nothing more over it
    assert tools[0].rstrip().endswith("+12 out · turn 38s · 1s")
    assert tools[1].rstrip().endswith("+0 out · turn 38s · 2m 08s")
    # the call still going keeps its running elapsed beside its typical time, and no turn
    assert "⋯ running" in tools[2]
    assert tools[2].rstrip().endswith("0s · ~1s")
    assert "turn" not in tools[2]


def test_note_shows_durations_before_the_fold_mark() -> None:
    blocks, _ = build_transcript_blocks(_durations_stream())
    called = blocks[1]
    note = transcript._note(called, hidden=3, is_open=False, to=None, breadth=Breadth.WIDE)
    assert note == "+12 out · turn 38s · 1s · ▸ 3 lines"
    opened = transcript._note(called, hidden=3, is_open=True, to=None, breadth=Breadth.WIDE)
    assert opened == "+12 out · turn 38s · 1s · ▾ 3 lines"


def test_note_shows_durations_alone_when_only_one_is_known() -> None:
    blocks, _ = build_transcript_blocks(_durations_stream())
    message = blocks[0]
    assert transcript._note(message, hidden=0, is_open=False, to=None, breadth=Breadth.NARROW) == ""
    lone = (
        _provider(1, FIRST, "requested", 0),
        _provider(2, FIRST, "result", 3),
    )
    (call,), _ = build_transcript_blocks(lone)
    assert transcript._note(call, hidden=0, is_open=False, to=None, breadth=Breadth.NARROW) == "3s"


def test_note_shows_durations_never_on_a_block_in_flight() -> None:
    """An in-flight block keeps its running elapsed note, measured to the reference instant."""
    blocks, _ = build_transcript_blocks(_durations_stream())
    going = blocks[-1]
    assert going.in_flight is True
    to = going.at + timedelta(seconds=5)
    assert transcript._note(going, hidden=0, is_open=False, to=to, breadth=Breadth.NARROW) == "5s"
