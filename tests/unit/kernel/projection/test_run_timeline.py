"""A Run's timeline rows: deterministic by sequence, P0 never folded, noise coalesced.

UI-008: the grouping is a function of the Run's sequence alone, so a replay that delivered
the lines out of order or twice reduces to the same groups and the same digest as a clean
read, and no P0 event is ever folded into another row. CON-076: a repeated low-priority
run of events is one row stating its kind, a count and a span; anything that is not the
same kind about the same target, in sequence, inside the window closes it.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from eawf.kernel.projection.run_timeline import (
    COALESCE_WINDOW_SECONDS,
    EVENT_PRIORITIES,
    EventPriority,
    reduce_timeline,
)
from eawf.kernel.runtime.events import QuarantineReason, RunEventKind, RunEventRecord

RUN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
T0: Final = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
FAMILY: Final = "pytest"


def _event(
    sequence: int,
    kind: RunEventKind,
    payload: dict[str, Any],
    *,
    at: int | None = None,
    quarantine: QuarantineReason | None = None,
) -> RunEventRecord:
    """Return one event line at ``sequence``, recorded ``at`` seconds after T0."""
    return RunEventRecord.model_validate(
        {
            "event_ref": f"EVT-{sequence:08x}",
            "run_ref": RUN,
            "run_sequence": sequence,
            "event_kind": kind.value,
            "provenance": "provider_native",
            "payload": payload,
            "actor": "OP-0001",
            "recorded_at": (T0 + timedelta(seconds=sequence if at is None else at)).isoformat(),
            "quarantine": None if quarantine is None else quarantine.value,
        }
    )


def _output(
    sequence: int, command: str = "CMD-0000000a", *, at: int | None = None
) -> RunEventRecord:
    return _event(
        sequence,
        RunEventKind.COMMAND_OUTPUT,
        {
            "payload_kind": "command",
            "command_family_ref": FAMILY,
            "command_ref": command,
            "phase": "output",
            "execution": "foreground",
            "stream": "stdout",
            "chunk_ref": f"artifact://chunks/{sequence}",
        },
        at=at,
    )


def _error(sequence: int) -> RunEventRecord:
    return _event(
        sequence,
        RunEventKind.ERROR_OBSERVED,
        {
            "payload_kind": "error",
            "code": "tool_failed",
            "retry_class": "never",
            "message": "the file does not exist",
        },
    )


def _thinking(sequence: int) -> RunEventRecord:
    return _event(
        sequence,
        RunEventKind.REASONING_STARTED,
        {"payload_kind": "reasoning_summary", "phase": "started"},
    )


def test_ui_008_every_event_kind_has_a_priority() -> None:
    assert set(EVENT_PRIORITIES) == set(RunEventKind)
    for kind in (RunEventKind.EVENT_GAP, RunEventKind.RUN_TRANSITION, RunEventKind.QUESTION_RAISED):
        assert EVENT_PRIORITIES[kind] is EventPriority.P0


def test_con_076_a_repeated_low_priority_run_is_one_row_with_kind_count_and_span() -> None:
    events = [_thinking(1), *(_output(n) for n in range(2, 14)), _error(14)]
    timeline = reduce_timeline(events)
    kinds = [(g.event_kind, g.count) for g in timeline.groups]
    assert kinds == [
        (RunEventKind.REASONING_STARTED, 1),
        (RunEventKind.COMMAND_OUTPUT, 12),
        (RunEventKind.ERROR_OBSERVED, 1),
    ]
    folded = timeline.groups[1]
    assert (folded.first_sequence, folded.last_sequence, folded.span_seconds) == (2, 13, 11)
    assert folded.coalesced
    assert timeline.event_count == 14


def test_ui_008_a_p0_event_is_never_folded_even_when_repeated() -> None:
    timeline = reduce_timeline([_error(n) for n in range(1, 6)])
    assert [g.count for g in timeline.groups] == [1] * 5
    assert timeline.p0_events == 5
    assert timeline.p0_coalesced == 0


def test_ui_008_a_replay_out_of_order_and_twice_has_the_clean_read_s_digest() -> None:
    clean = [_thinking(1), *(_output(n) for n in range(2, 9)), _error(9), _output(10)]
    replay = [*clean, *clean[3:6]]
    random.Random(7).shuffle(replay)
    assert reduce_timeline(replay).digest == reduce_timeline(clean).digest
    assert reduce_timeline(replay).groups == reduce_timeline(clean).groups


def test_con_076_another_target_or_a_broken_sequence_closes_the_group() -> None:
    other = reduce_timeline([_output(1), _output(2), _output(3, "CMD-0000000b"), _output(4)])
    assert [g.count for g in other.groups] == [2, 1, 1]
    gapped = reduce_timeline([_output(1), _output(2), _output(4)])
    assert [g.count for g in gapped.groups] == [2, 1]


def test_con_076_the_window_closes_a_group_that_ran_too_long() -> None:
    late = COALESCE_WINDOW_SECONDS + 1
    timeline = reduce_timeline([_output(1, at=0), _output(2, at=1), _output(3, at=late)])
    assert [g.count for g in timeline.groups] == [2, 1]


def test_ui_008_a_quarantined_line_is_left_out() -> None:
    held = _event(
        3,
        RunEventKind.REASONING_STARTED,
        {"payload_kind": "reasoning_summary", "phase": "started"},
        quarantine=QuarantineReason.SEQUENCE_CONFLICT,
    )
    timeline = reduce_timeline([_output(1), _output(2), held])
    assert timeline.event_count == 2
    assert [g.event_kind for g in timeline.groups] == [RunEventKind.COMMAND_OUTPUT]


@pytest.mark.parametrize("count", [0, 1])
def test_ui_008_an_empty_or_single_stream_is_its_own_timeline(count: int) -> None:
    timeline = reduce_timeline([_output(1)][:count])
    assert len(timeline.groups) == count
    assert timeline.event_count == count
    assert timeline.p0_coalesced == 0
