"""One transcript row per tool call, updated in place as the call moves.

A gateway call writes a requested, an accepted and a result line, and a provider's own
tool a requested and a result line, all sharing one ``call_ref``. The transcript folds
them into one block at the request's position and time that names the tool and where
the call stands now, so a reader scans calls rather than lifecycle steps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from eawf.kernel.projection.transcript import build_transcript_blocks
from eawf.kernel.runtime.content import ResolvedContent
from eawf.kernel.runtime.events import RunEventKind, RunEventRecord

RUN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
T0: Final = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
CALL: Final = "call-00000000000000a1"
OTHER: Final = "call-00000000000000a2"
RECEIPT: Final = "receipt-00000000000000a1"

#: The event kind each tool phase is written under.
PHASE_KINDS: Final = {
    "requested": RunEventKind.TOOL_REQUESTED,
    "accepted": RunEventKind.TOOL_ACCEPTED,
    "result": RunEventKind.TOOL_RESULT,
}


def _event(sequence: int, payload: dict[str, Any], *, quarantined: bool = False) -> RunEventRecord:
    """Return one tool line at ``sequence``, recorded ``sequence`` seconds after T0."""
    return RunEventRecord.model_validate(
        {
            "event_ref": f"EVT-{sequence:08x}",
            "run_ref": RUN,
            "run_sequence": sequence,
            "event_kind": PHASE_KINDS[payload["phase"]].value,
            "provenance": "provider_native",
            "payload": payload,
            "actor": "OP-0001",
            "recorded_at": (T0 + timedelta(seconds=sequence)).isoformat(),
            "quarantine": "sequence_conflict" if quarantined else None,
        }
    )


def _gateway(sequence: int, phase: str, call: str = CALL, **outcome: Any) -> RunEventRecord:
    payload = {"payload_kind": "tool", "call_ref": call, "tool_id": "repo_read", "phase": phase}
    return _event(sequence, payload | outcome)


def _provider(
    sequence: int, phase: str, call: str = CALL, outcome: str | None = None
) -> RunEventRecord:
    payload = {
        "payload_kind": "provider_tool",
        "call_ref": call,
        "tool_name": "grep",
        "tool_class": "search",
        "phase": phase,
        "outcome": outcome,
    }
    return _event(sequence, payload)


def _texts(events: tuple[RunEventRecord, ...], **kwargs: Any) -> list[str | None]:
    blocks, _ = build_transcript_blocks(events, **kwargs)
    return [block.text.value for block in blocks]


@pytest.mark.parametrize(
    ("phases", "status"),
    [
        (("requested",), "requested"),
        (("requested", "accepted"), "accepted"),
        (("requested", "accepted", "result"), "succeeded"),
    ],
)
def test_one_row_per_call_reads_the_latest_status(phases: tuple[str, ...], status: str) -> None:
    """Every line of one call draws one row naming the tool and its latest status."""
    events = tuple(
        _gateway(index + 1, phase, **({"result_ref": RECEIPT} if phase == "result" else {}))
        for index, phase in enumerate(phases)
    )
    blocks, _ = build_transcript_blocks(events)
    assert len(blocks) == 1
    assert blocks[0].text.value == f"repo_read · {status}"
    assert blocks[0].sequence == 1
    assert blocks[0].at == T0 + timedelta(seconds=1)
    assert blocks[0].kind is PHASE_KINDS[phases[-1]]
    # a gateway call holds the Run from its acceptance until its result lands
    assert blocks[0].in_flight is (phases[-1] == "accepted")


def test_one_row_per_call_reads_failed_with_the_error() -> None:
    """A result naming an error reads ``failed`` and carries the error as its body."""
    events = (
        _gateway(1, "requested"),
        _gateway(2, "accepted"),
        _gateway(3, "result", error_code="SCOPE_DENIED"),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [block.text.value for block in blocks] == ["repo_read · failed"]
    assert blocks[0].body == (("ERROR", "SCOPE_DENIED"),)
    assert not blocks[0].in_flight


def test_one_row_per_call_carries_the_result_content() -> None:
    """The folded row unfolds to what the result's call resolved to."""
    events = (_gateway(1, "requested"), _gateway(2, "result", result_ref=RECEIPT))
    held = ResolvedContent(
        ref=CALL, lines=("line one", "line two"), total_lines=2, withheld_lines=0
    )
    blocks, _ = build_transcript_blocks(events, contents={CALL: held})
    assert blocks[0].body == (("OUTPUT", "line one"), ("", "line two"))


def test_one_row_per_call_keeps_interleaved_calls_apart() -> None:
    """Two calls whose lines interleave draw two rows, each at its own request."""
    events = (
        _gateway(1, "requested"),
        _gateway(2, "requested", OTHER),
        _gateway(3, "result", result_ref=RECEIPT),
        _gateway(4, "accepted", OTHER),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [(block.sequence, block.text.value) for block in blocks] == [
        (1, "repo_read · succeeded"),
        (2, "repo_read · accepted"),
    ]


def test_one_row_per_call_folds_lines_given_out_of_order() -> None:
    """Lines handed in out of order still fold by sequence, the latest one deciding."""
    events = (_gateway(3, "result", result_ref=RECEIPT), _gateway(1, "requested"))
    assert _texts(events) == ["repo_read · succeeded"]


def test_one_row_per_call_skips_a_quarantined_result() -> None:
    """A quarantined result line is no part of the call, so the row stays in flight."""
    events = (
        _gateway(1, "requested"),
        _gateway(2, "accepted"),
        _event(
            3,
            {
                "payload_kind": "tool",
                "call_ref": CALL,
                "tool_id": "repo_read",
                "phase": "result",
                "result_ref": RECEIPT,
            },
            quarantined=True,
        ),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [block.text.value for block in blocks] == ["repo_read · accepted"]
    assert blocks[0].in_flight


def test_one_row_per_call_is_not_in_flight_once_the_run_ended() -> None:
    """An ended Run holds no call in flight, whatever line its stream stopped on."""
    events = (_gateway(1, "requested"), _gateway(2, "accepted"))
    blocks, _ = build_transcript_blocks(events, ended=True)
    assert not blocks[0].in_flight


def test_one_row_per_call_draws_nothing_for_an_empty_stream() -> None:
    """A Run with no lines draws no row."""
    assert build_transcript_blocks(()) == ((), ())


@pytest.mark.parametrize(
    ("outcome", "status"), [(None, "requested"), ("ok", "succeeded"), ("error", "failed")]
)
def test_provider_call_row_reads_the_latest_status(outcome: str | None, status: str) -> None:
    """A provider call the gateway did not echo folds by call, then reads its outcome."""
    events: tuple[RunEventRecord, ...] = (_provider(1, "requested"),)
    if outcome is not None:
        events += (_provider(2, "result", outcome=outcome),)
    blocks, _ = build_transcript_blocks(events)
    assert [block.text.value for block in blocks] == [f"grep · {status}"]
    assert blocks[0].sequence == 1
    assert blocks[0].in_flight is (outcome is None)


def test_provider_call_row_keeps_calls_of_one_tool_apart() -> None:
    """Two calls of one provider tool draw two rows, each settled by its own result."""
    events = (
        _provider(1, "requested"),
        _provider(2, "requested", OTHER),
        _provider(3, "result", OTHER, outcome="error"),
    )
    blocks, _ = build_transcript_blocks(events)
    assert [(block.text.value, block.in_flight) for block in blocks] == [
        ("grep · requested", True),
        ("grep · failed", False),
    ]


def test_provider_call_row_rejects_a_result_with_no_outcome() -> None:
    """A provider result that names no outcome is refused at the boundary."""
    with pytest.raises(ValueError, match="names its outcome"):
        _provider(1, "result")
