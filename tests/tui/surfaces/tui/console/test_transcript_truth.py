"""The transcript and the Run frames tell the truth about one real native turn.

The fixture is the event stream of one native Claude Run as the daemon recorded it: a
usage reading, a reasoning start, a message, a call to eawf's own semantic server seen by
both the provider and the gateway, more usage, a closing message. Each test pins one way
the console misread that stream: usage drawn as text it could not state, each semantic
call drawn twice, a closed reasoning turn read as still open, the state row reading
unknown on an ended Run, the provider never recorded, timeline rows saying only their
sequence, and a RUNNING Run said to have nothing in flight.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import RouteProjection, build_route_projection
from eawf.kernel.projection.run_timeline import TURN_OPENED, reduce_timeline
from eawf.kernel.projection.transcript import (
    CLOSED_TURN_TEXT,
    NO_TEXT_REASON,
    OPEN_TURN_TEXT,
    SEMANTIC_TOOL_PREFIX,
    TRANSCRIPT_ROUTE,
    TranscriptReadModel,
    block_text,
    build_transcript_blocks,
    build_transcript_view,
)
from eawf.kernel.runtime.events import (
    EventGapPayload,
    ProviderToolPayload,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
)
from eawf.kernel.runtime.usage import UsagePayload
from eawf.runtime.mcp.semantic_stdio import SERVER_NAME
from eawf.runtime.runtimes.transcript_items import provider_tool_name
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route, transcript
from eawf.surfaces.tui.console.renderers.run_detail import timeline_rows
from eawf.surfaces.tui.console.session import Session
from tests.unit.runtime.runtimes.claude.test_sdk_launcher import (
    _launch_and_turn,
    _launcher,
    _request,
    workspace,
)

__all__ = ["workspace"]

#: The recorded event lines of one native Claude Run, scrubbed of nothing but its paths.
TURN = Path(__file__).resolve().parents[4] / "fixtures/console/transcript/native_turn.jsonl"

#: The tracked prototype registers the frame renders around.
FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

RUN_KEY = "RUN-00000421"
RUN_URN = f"eawf://EAWF/EAWF/EAWF/run/{RUN_KEY}"
AT = datetime(2026, 10, 6, 22, 5, tzinfo=UTC)


def _turn() -> tuple[RunEventRecord, ...]:
    """Return the recorded turn's event lines, in the order the ledger holds them."""
    lines = TURN.read_text(encoding="utf-8").splitlines()
    return tuple(RunEventRecord.model_validate_json(line) for line in lines if line)


def _projection(status: str) -> RouteProjection:
    """Return the transcript projection of the one Run, stored at ``status``."""
    document = {"run": {RUN_KEY: {"urn": RUN_URN, "revision": 3, "status": status}}}
    return build_route_projection(
        route=TRANSCRIPT_ROUTE, document=document, cursor=41, scope_id="EAWF", generated_at=AT
    )


def _model(
    events: tuple[RunEventRecord, ...] | None = None, *, status: str = "COMPLETED"
) -> TranscriptReadModel:
    return build_transcript_view(_projection(status), events=_turn() if events is None else events)


def _fixture() -> Fixture:
    return load_fixture(FIXTURE_ROOT)


def _view(model: TranscriptReadModel | None = None, *, width: int = 160) -> View:
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.subj_id = RUN_KEY
    session.follow = True
    return View(session=session, fixture=_fixture(), w=width, h=40, projection=model)


def _frame(model: TranscriptReadModel, *, width: int = 160) -> str:
    return "\n".join(render_route(_view(model, width=width)))


def _line(sequence: int, kind: RunEventKind, payload: Any, **fields: Any) -> RunEventRecord:
    """Return one event line of the Run at ``sequence``."""
    record: dict[str, Any] = {
        "event_ref": f"EVT-{sequence:032x}",
        "run_ref": RUN_URN,
        "run_sequence": sequence,
        "event_kind": kind,
        "provenance": "provider_native",
        "payload": payload,
        "actor": "OP-0001",
        "recorded_at": AT + timedelta(seconds=sequence),
    }
    return RunEventRecord.model_validate(record | fields)


def _usage(**fields: Any) -> UsagePayload:
    base: dict[str, Any] = {
        "usage_source": "provider_transcript",
        "is_cumulative": True,
        "measurement_quality": "measured",
    }
    return UsagePayload.model_validate(base | fields)


def test_the_fixture_is_the_recorded_native_turn() -> None:
    """The stream the rest pins: eleven lines, both sides of one semantic call."""
    events = _turn()
    assert [event.run_sequence for event in events] == list(range(1, 12))
    kinds = {type(event.payload) for event in events}
    assert {ProviderToolPayload, ToolPayload, UsagePayload, ReasoningSummaryPayload} <= kinds


# ---------- 1: a usage reading states its counters ----------


def test_every_usage_reading_states_its_counters_in_the_runs_totals() -> None:
    """The three readings open no block; the latest running total ends the STATE row."""
    model = _model()
    assert all(block.kind is not RunEventKind.USAGE_OBSERVED for block in model.blocks)
    frame = _frame(model)
    assert NO_TEXT_REASON.split(" the")[0] not in frame
    assert "this event kind states no text" not in frame
    assert "# usage" not in frame
    state = next(row for row in frame.split("\n") if " STATE " in row).rstrip()
    assert state.endswith("in 3 · out 1,014 · cache 11,539 · cost unpriced")


def test_a_usage_reading_states_only_the_counters_it_carries() -> None:
    """Boundary: one counter, a delta reading, a priced cost."""
    priced = _usage(output_tokens=1, is_cumulative=False, cost_microusd=1200, price_source="billed")
    text = block_text(_line(1, RunEventKind.USAGE_OBSERVED, priced)).value
    assert text == "1 out tokens · since the last reading · 0.0012 USD · measured"


def test_an_unavailable_usage_reading_says_no_counter_was_reported() -> None:
    """Empty: a reading with no counter says so instead of drawing zero."""
    empty = _usage(measurement_quality="unavailable")
    text = block_text(_line(1, RunEventKind.USAGE_OBSERVED, empty)).value
    assert text is not None
    assert text.startswith("no token counter reported")


def test_a_usage_reading_with_a_counter_but_unavailable_quality_is_refused() -> None:
    """Error path: the payload boundary refuses a reading that contradicts itself."""
    with pytest.raises(ValueError, match="unavailable reading carries no counters"):
        _usage(measurement_quality="unavailable", input_tokens=1)


# ---------- 2: a semantic call is drawn once ----------


def test_the_semantic_prefix_is_the_launchers_spelling() -> None:
    assert provider_tool_name(f"mcp__{SERVER_NAME}__submit_report") == (
        f"{SEMANTIC_TOOL_PREFIX}submit_report"
    )


def test_a_semantic_tool_call_is_drawn_once_under_the_gateways_lines() -> None:
    model = _model()
    tools = [block for block in model.blocks if block.lane == "tool"]
    # the gateway's requested, accepted and result lines fold into the call's one row
    assert [block.text.value for block in tools] == ["submit_report · succeeded"]
    assert SEMANTIC_TOOL_PREFIX not in _frame(model)


def test_a_semantic_call_the_gateway_never_recorded_is_still_drawn() -> None:
    """Off-by-one: two provider calls, one gateway call; the unmatched call stays."""
    name = f"{SEMANTIC_TOOL_PREFIX}submit_report"
    lines = (
        _line(
            1,
            RunEventKind.TOOL_REQUESTED,
            ProviderToolPayload(
                call_ref="call-0000000000000001",
                tool_name=name,
                tool_class="other",
                phase="requested",
            ),
        ),
        _line(
            2,
            RunEventKind.TOOL_ACCEPTED,
            ToolPayload(
                call_ref="call-0000000000000009",
                tool_id="submit_report",
                phase="accepted",
            ),
            provenance="daemon_observed",
        ),
        _line(
            3,
            RunEventKind.TOOL_REQUESTED,
            ProviderToolPayload(
                call_ref="call-0000000000000002",
                tool_name=name,
                tool_class="other",
                phase="requested",
            ),
        ),
    )
    blocks, _ = build_transcript_blocks(lines)
    assert [block.sequence for block in blocks] == [2, 3]


def test_a_provider_tool_outside_the_semantic_server_is_kept() -> None:
    lines = (
        _line(
            1,
            RunEventKind.TOOL_REQUESTED,
            ProviderToolPayload(
                call_ref="call-0000000000000001",
                tool_name="bash",
                tool_class="command",
                phase="requested",
            ),
        ),
        _line(
            2,
            RunEventKind.TOOL_ACCEPTED,
            ToolPayload(
                call_ref="call-0000000000000009",
                tool_id="submit_report",
                phase="accepted",
            ),
            provenance="daemon_observed",
        ),
    )
    blocks, _ = build_transcript_blocks(lines)
    assert [block.sequence for block in blocks] == [1, 2]


# ---------- 3: a closed reasoning turn reads as closed ----------


def test_a_reasoning_turn_the_next_message_closed_reads_as_closed() -> None:
    model = _model()
    (thinking,) = [block for block in model.blocks if block.lane == "thinking"]
    assert not thinking.in_flight
    assert thinking.text.value == CLOSED_TURN_TEXT
    assert "has not been summarized" not in _frame(model)


def test_an_ended_run_holds_no_open_turn_whatever_line_it_ended_on() -> None:
    """Boundary: the last line opened a turn, and the Run's stored status ended it."""
    events = (
        *_turn(),
        _line(12, RunEventKind.REASONING_STARTED, ReasoningSummaryPayload(phase="started")),
    )
    model = _model(events)
    assert not model.is_thinking()
    assert not any(block.in_flight for block in model.blocks)
    assert model.blocks[-1].text.value == CLOSED_TURN_TEXT


def test_a_running_runs_open_turn_still_reads_as_open() -> None:
    events = (
        *_turn(),
        _line(12, RunEventKind.REASONING_STARTED, ReasoningSummaryPayload(phase="started")),
    )
    model = _model(events, status="RUNNING")
    assert model.is_thinking()
    assert model.blocks[-1].in_flight
    assert model.blocks[-1].text.value == OPEN_TURN_TEXT


# ---------- 4: the state row states the Run's status ----------


def _state_row(frame: str) -> str:
    return next(line for line in frame.splitlines() if line.startswith(" STATE"))


def test_the_state_row_of_a_completed_run_states_its_status() -> None:
    row = _state_row(_frame(_model()))
    assert "COMPLETED" in row
    assert transcript.NOT_THINKING in row
    assert "? unknown" not in row


def test_the_state_row_of_a_thinking_run_states_the_turn() -> None:
    events = (
        *_turn(),
        _line(12, RunEventKind.REASONING_STARTED, ReasoningSummaryPayload(phase="started")),
    )
    row = _state_row(_frame(_model(events, status="RUNNING")))
    assert "thinking" in row
    assert transcript.NOT_THINKING not in row


def test_the_state_row_of_an_unheld_run_still_says_unknown() -> None:
    """Missing key: a Run the register does not hold keeps the unknown cell."""
    model = _model()
    view = _view(model)
    view.session.subj_id = "RUN-99999999"
    row = _state_row("\n".join(render_route(view)))
    assert "? unknown" in row


# ---------- 5: the Claude launcher records who serves the model ----------


def test_a_claude_launch_records_anthropic_as_the_provider(workspace: Path) -> None:
    outcome, _turn_outcome = asyncio.run(
        _launch_and_turn(_launcher(workspace), _request(workspace))
    )
    assert outcome.runtime_tuple is not None
    assert outcome.runtime_tuple.provider == "anthropic"


def test_a_claude_launch_through_a_gateway_records_no_provider(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base URL the child is routed through names no upstream, so none is guessed."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.invalid")
    outcome, _turn_outcome = asyncio.run(
        _launch_and_turn(_launcher(workspace), _request(workspace))
    )
    assert outcome.runtime_tuple is not None
    assert outcome.runtime_tuple.provider is None


# ---------- 6: a timeline row says what its event means ----------


def test_timeline_rows_state_what_each_event_says() -> None:
    timeline = reduce_timeline(_turn())
    rows = "\n".join(str(row) for row in timeline_rows(_view(width=160), timeline))
    assert "Report filed." in rows
    assert "11,539 cache tokens" in rows
    assert "sequence 10" not in rows
    details = [group.detail for group in timeline.groups]
    assert TURN_OPENED in details
    assert all(detail is not None for detail in details)


def test_a_timeline_row_with_no_sentence_falls_back_to_its_sequence() -> None:
    """Empty: a gap states no sentence, so its row still names where it sits."""
    gap = _line(
        1,
        RunEventKind.EVENT_GAP,
        EventGapPayload(
            expected_sequence=1,
            observed_sequence=3,
            replay_requested=False,
            replay_capability_state="unavailable",
            gap_ref="GAP-00000001",
        ),
        provenance="daemon_observed",
    )
    timeline = reduce_timeline((gap,))
    assert timeline.groups[0].detail is None
    rows = "\n".join(str(row) for row in timeline_rows(_view(width=160), timeline))
    assert "sequence 1" in rows


# ---------- 7: a RUNNING Run is in flight ----------


def test_a_running_run_between_blocks_is_in_flight() -> None:
    model = _model(status="RUNNING")
    context = transcript.native_context(_view(model), model)
    assert "RUNNING" in context
    assert f"{transcript.RUN_IN_FLIGHT} · no block open" in context
    assert "nothing in flight" not in context


def test_a_completed_run_has_nothing_in_flight() -> None:
    model = _model()
    context = transcript.native_context(_view(model), model)
    assert "COMPLETED · nothing in flight" in context
