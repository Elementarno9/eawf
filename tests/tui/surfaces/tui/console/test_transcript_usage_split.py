"""Usage leaves the transcript's rows: the Run's totals head the frame, each turn its delta.

A usage reading is a running total the provider restates after every response, so a row
per reading repeated one growing number down the feed. The readings now open no block:
the read model adds them up into the Run's totals, which the STATE row ends on, and the
block that carries a model turn's seconds also states the input and output tokens that
turn spent over the reading the previous turn was drawn with.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.transcript import build_transcript_blocks
from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind, RunEventRecord
from eawf.kernel.runtime.usage import UsagePayload, UsageTotals
from eawf.surfaces.tui.console.frame import Breadth
from eawf.surfaces.tui.console.renderers import transcript
from tests.tui.surfaces.tui.console import test_transcript_blocks as tb


def _line(sequence: int, kind: RunEventKind, payload: Any, at: int) -> RunEventRecord:
    """Return one line ``at`` seconds after the probe's anchor."""
    return tb._event(sequence, event_kind=kind, payload=payload).model_copy(
        update={"recorded_at": tb.AT + timedelta(seconds=at)}
    )


def _said(sequence: int, at: int) -> RunEventRecord:
    payload = MessageSummaryPayload(message_role="assistant", summary=f"said {sequence}")
    return _line(sequence, RunEventKind.MESSAGE_SUMMARIZED, payload, at)


def _usage(sequence: int, at: int, *, cumulative: bool = True, **counters: Any) -> RunEventRecord:
    """Return one usage reading carrying ``counters``, a running total by default."""
    fields: dict[str, Any] = {
        "usage_source": "provider_transcript",
        "is_cumulative": cumulative,
        "measurement_quality": "measured",
    }
    payload = UsagePayload.model_validate(fields | counters)
    return _line(sequence, RunEventKind.USAGE_OBSERVED, payload, at)


def _state_row(events: tuple[RunEventRecord, ...]) -> str:
    """Return the STATE row of the frame drawn over ``events``, right padding dropped."""
    frame = tb._frame(tb._view(events), width=160)
    return next(row for row in frame.split("\n") if " STATE " in row).rstrip()


def _note(block: Any) -> str:
    return transcript._note(block, hidden=0, is_open=False, to=None, breadth=Breadth.NARROW)


# ---- a usage reading opens no block --------------------------------------------------


def test_no_usage_rows_leave_a_reading_out_of_the_block_list() -> None:
    events = (_said(1, 0), _usage(2, 3, output_tokens=12), _said(3, 4))
    blocks, _ = build_transcript_blocks(events)
    assert [block.sequence for block in blocks] == [1, 3]
    assert all(block.kind is not RunEventKind.USAGE_OBSERVED for block in blocks)
    assert "usage" not in {block.lane for block in blocks}


def test_no_usage_rows_a_lone_reading_draws_no_block() -> None:
    """Single: a stream of one reading draws no block, and the frame says so."""
    model = tb._view((_usage(1, 0, input_tokens=5),))
    assert model.blocks == ()
    assert transcript.NO_BLOCK in tb._frame(model, width=160)


def test_no_usage_rows_in_the_drawn_frame() -> None:
    events = (
        _said(1, 0),
        _usage(2, 3, output_tokens=12),
        _said(3, 4),
        _usage(4, 5, input_tokens=1),
    )
    frame = tb._frame(tb._view(events), width=160)
    assert "# usage" not in frame
    assert "running total" not in frame
    assert "usage" not in transcript.NATIVE_GLYPH


def test_no_usage_rows_read_model_carries_the_latest_running_total() -> None:
    events = (
        _said(1, 0),
        _usage(2, 3, input_tokens=2, output_tokens=8, cache_tokens=4962),
        _said(3, 4),
        _usage(4, 5, input_tokens=3, output_tokens=1014, cache_tokens=11539),
    )
    assert tb._view(events).usage == UsageTotals(
        input_tokens=3, output_tokens=1014, cache_tokens=11539, cost_microusd=None
    )


def test_no_usage_rows_read_model_carries_the_cost_in_micro_usd() -> None:
    reading = _usage(1, 0, output_tokens=1, cost_microusd=123400, price_source="billed")
    usage = tb._view((reading,)).usage
    assert usage is not None
    assert usage.cost_microusd == 123400
    assert usage.input_tokens is None


def test_no_usage_rows_read_model_totals_are_none_before_any_reading() -> None:
    """Empty: a Run with no line, and one with lines but no reading, carry no totals."""
    assert tb._view(()).usage is None
    assert tb._view((_said(1, 0),)).usage is None


def test_no_usage_rows_ignore_a_quarantined_reading() -> None:
    quarantined = _usage(2, 3, input_tokens=99).model_copy(
        update={"quarantine": tb.QuarantineReason.LATE_AFTER_TERMINAL}
    )
    model = tb._view((_said(1, 0), quarantined))
    assert model.usage is None


def test_no_usage_rows_refuse_a_negative_counter_at_the_payload_boundary() -> None:
    """Error path: a reading cannot state a negative count, so no total goes below zero."""
    with pytest.raises(ValidationError):
        UsagePayload.model_validate(
            {
                "usage_source": "provider_transcript",
                "is_cumulative": True,
                "measurement_quality": "measured",
                "input_tokens": -1,
            }
        )


# ---- the STATE row ends on the Run's totals ------------------------------------------


def test_state_totals_end_the_state_row() -> None:
    events = (
        _said(1, 0),
        _usage(
            2,
            3,
            input_tokens=12345,
            output_tokens=1234,
            cache_tokens=456789,
            cost_microusd=123400,
            price_source="billed",
        ),
    )
    assert _state_row(events).endswith("in 12,345 · out 1,234 · cache 456,789 · cost 0.1234 USD")


def test_state_totals_say_unpriced_when_no_reading_priced_the_spend() -> None:
    events = (_said(1, 0), _usage(2, 3, input_tokens=3, output_tokens=1014, cache_tokens=11539))
    assert _state_row(events).endswith("in 3 · out 1,014 · cache 11,539 · cost unpriced")


def test_state_totals_leave_out_a_counter_no_reading_reported() -> None:
    events = (_said(1, 0), _usage(2, 3, output_tokens=12))
    row = _state_row(events)
    assert row.endswith("contiguous through 2 · out 12 · cost unpriced")


def test_state_totals_of_an_unavailable_reading_state_only_the_cost() -> None:
    """Boundary: a reading of no counter adds up to nothing but an unpriced cost."""
    reading = _usage(2, 3, measurement_quality="unavailable")
    assert _state_row((_said(1, 0), reading)).endswith("contiguous through 2 · cost unpriced")


def test_state_totals_say_usage_was_not_read_before_any_reading() -> None:
    assert _state_row((_said(1, 0),)).endswith(f"· {transcript.USAGE_NOT_READ}")
    assert _state_row(()).endswith(f"· {transcript.USAGE_NOT_READ}")


def test_state_totals_add_a_delta_reading_to_the_running_total() -> None:
    events = (
        _said(1, 0),
        _usage(2, 3, input_tokens=100),
        _usage(3, 4, cumulative=False, input_tokens=20),
    )
    assert _state_row(events).endswith("in 120 · cost unpriced")


def test_state_totals_refuse_a_total_that_is_not_a_count() -> None:
    """Error path: the totals the row draws are validated counts, never a string."""
    with pytest.raises(ValidationError):
        UsageTotals.model_validate({"input_tokens": "12"})


# ---- each turn states the tokens it spent --------------------------------------------


def test_turn_delta_of_the_first_turn_is_the_reading_itself() -> None:
    events = (_said(1, 0), _usage(2, 38, input_tokens=1200, output_tokens=85), _said(3, 40))
    blocks, _ = build_transcript_blocks(events)
    turn = blocks[1]
    assert (turn.turn_seconds, turn.turn_input_tokens, turn.turn_output_tokens) == (38, 1200, 85)
    assert _note(turn) == "+1,200 in · +85 out · turn 38s"


def test_turn_delta_runs_against_the_previous_running_total() -> None:
    events = (
        _said(1, 0),
        _usage(2, 10, input_tokens=1000, output_tokens=50),
        _said(3, 11),
        _usage(4, 49, input_tokens=2200, output_tokens=135),
        _said(5, 50),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [(b.turn_input_tokens, b.turn_output_tokens) for b in blocks] == [
        (None, None),
        (1000, 50),
        (1200, 85),
    ]
    # no tool returned yet, so the turn runs from the Run's first line
    assert _note(blocks[2]) == "+1,200 in · +85 out · turn 49s"


def test_turn_delta_of_a_reading_that_is_not_a_running_total_is_the_reading() -> None:
    events = (
        _said(1, 0),
        _usage(2, 10, input_tokens=1000, output_tokens=50),
        _said(3, 11),
        _usage(4, 49, cumulative=False, input_tokens=1200, output_tokens=85),
        _said(5, 50),
    )
    blocks, _ = build_transcript_blocks(events)
    assert (blocks[2].turn_input_tokens, blocks[2].turn_output_tokens) == (1200, 85)


def test_turn_delta_of_readings_with_no_block_between_runs_to_the_latest() -> None:
    """Readings back to back close one turn: its tokens run from the reading the turn
    before it was drawn with to the latest, not from the reading just before."""
    events = (
        _said(1, 0),
        _usage(2, 10, input_tokens=100, output_tokens=10),
        _said(3, 11),
        _usage(4, 20, input_tokens=150, output_tokens=15),
        _usage(5, 30, input_tokens=300, output_tokens=30),
        _said(6, 31),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [block.sequence for block in blocks] == [1, 3, 6]
    assert (blocks[2].turn_seconds, blocks[2].turn_input_tokens) == (30, 200)
    assert blocks[2].turn_output_tokens == 20


def test_turn_delta_of_delta_readings_with_no_block_between_sums_them() -> None:
    """Two readings that are each a delta both count, as the Run's totals add them."""
    events = (
        _said(1, 0),
        _usage(2, 10, cumulative=False, input_tokens=10, output_tokens=4),
        _usage(3, 20, cumulative=False, input_tokens=7, output_tokens=1),
        _said(4, 21),
    )
    blocks, _ = build_transcript_blocks(events)
    assert (blocks[1].turn_input_tokens, blocks[1].turn_output_tokens) == (17, 5)


def test_turn_delta_of_a_restarted_running_total_opens_a_new_series() -> None:
    """A running total below the one before it restarted, so it counts whole, never negative."""
    events = (
        _said(1, 0),
        _usage(2, 10, input_tokens=100),
        _said(3, 11),
        _usage(4, 20, input_tokens=30),
        _said(5, 21),
    )
    blocks, _ = build_transcript_blocks(events)
    assert blocks[2].turn_input_tokens == 30


def test_turn_delta_of_a_restart_inside_one_turn_adds_both_series() -> None:
    """Boundary: the old series' gain and the new series' total both belong to the turn."""
    events = (
        _said(1, 0),
        _usage(2, 10, input_tokens=100),
        _said(3, 11),
        _usage(4, 20, input_tokens=120),
        _usage(5, 30, input_tokens=15),
        _said(6, 31),
    )
    blocks, _ = build_transcript_blocks(events)
    assert blocks[2].turn_input_tokens == 35


def test_turn_delta_leaves_out_a_counter_the_reading_did_not_report() -> None:
    events = (_said(1, 0), _usage(2, 38, output_tokens=85), _said(3, 40))
    blocks, _ = build_transcript_blocks(events)
    assert blocks[1].turn_input_tokens is None
    assert _note(blocks[1]) == "+85 out · turn 38s"


def test_turn_delta_of_an_unchanged_total_is_zero() -> None:
    """Off by one: a turn that spent nothing states +0, not nothing."""
    events = (
        _said(1, 0),
        _usage(2, 5, input_tokens=7, output_tokens=9),
        _said(3, 6),
        _usage(4, 7, input_tokens=7, output_tokens=9),
        _said(5, 8),
    )
    blocks, _ = build_transcript_blocks(events)
    assert _note(blocks[2]) == "+0 in · +0 out · turn 7s"


def test_turn_delta_after_a_trailing_reading_is_on_no_block() -> None:
    blocks, _ = build_transcript_blocks((_said(1, 0), _usage(2, 9, input_tokens=4)))
    assert [(b.turn_input_tokens, b.turn_output_tokens, b.turn_seconds) for b in blocks] == [
        (None, None, None)
    ]


def test_turn_delta_is_on_no_block_of_an_empty_stream() -> None:
    assert build_transcript_blocks(()) == ((), ())


def test_turn_delta_rides_the_drawn_row_beside_its_call() -> None:
    """The delta is drawn before the turn and the call's own seconds on the row."""
    call = "call-00000000000000a1"
    events = (
        _said(1, 0),
        _usage(2, 38, input_tokens=1200, output_tokens=85),
        _line(
            3,
            RunEventKind.TOOL_REQUESTED,
            tb.ProviderToolPayload.model_validate(
                {
                    "call_ref": call,
                    "tool_name": "bash",
                    "tool_class": "command",
                    "phase": "requested",
                }
            ),
            40,
        ),
        _line(
            4,
            RunEventKind.TOOL_RESULT,
            tb.ProviderToolPayload.model_validate(
                {
                    "call_ref": call,
                    "tool_name": "bash",
                    "tool_class": "command",
                    "phase": "result",
                    "outcome": "ok",
                }
            ),
            41,
        ),
    )
    rows = tb._frame(tb._view(events), width=160).split("\n")
    (row,) = [row for row in rows if "bash · " in row]
    assert row.rstrip().endswith("+1,200 in · +85 out · turn 38s · 1s")
