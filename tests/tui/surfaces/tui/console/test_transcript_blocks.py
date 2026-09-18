"""The transcript draws a Run's ordered event log, its holes, and a derived thinking state.

One route leaves the prototype registers here, and three claims about it are worth a test
each.

*The order is the stream's, not the file's.* Event lines arrive in ledger order, which is
arrival order, and a retried line arrives out of sequence. The blocks are sorted by
``run_sequence`` so the transcript reads as the episode happened, and quarantined lines --
retained as diagnostics and derived from by nothing -- never appear in the run at all.

*A hole is drawn.* A recorded gap covers a range the daemon never received. The range
becomes a block of its own saying how many sequences are missing, in the position it
occupies, so the transcript is never quietly shorter than the episode it claims to show.

*The thinking state is derived and says so.* No event states that an agent is thinking:
what exists is a reasoning turn that opened and has not been summarized. The transcript
folds the state out of that pairing, carries it as a derived truth field, and prints the
label beside it, so a frame never presents the inference as an observation.

The last group is the one the coverage grid owes. ``transcript`` is out of the manifest's
hole list in the same commit that binds it, and this suite checks the grid and the console
agree in both directions rather than taking the grid's word for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.operations import build_operations_view
from eawf.kernel.projection.transcript import (
    BLOCK_REVISION,
    NO_OPEN_TURN_REASON,
    NO_TEXT_REASON,
    OPEN_TURN_TEXT,
    PURGED_REASON,
    RUN_OUTCOME_PRODUCER,
    THINKING,
    TRANSCRIPT_FIELDS,
    TRANSCRIPT_ROUTE,
    TRANSCRIPT_ROUTES,
    TranscriptReadModel,
    block_text,
    build_transcript_view,
    open_reasoning_turn,
)
from eawf.kernel.projection.truth import TruthKind, TruthState
from eawf.kernel.runtime.events import (
    CommandPayload,
    EventGapPayload,
    QuarantineReason,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route, transcript
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console.test_coverage_grid_manifest import (
    coverage_defects,
    load_manifest,
)

#: Where the tracked prototype registers live.
FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: When the probe projections and event lines are stamped.
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
OTHER_RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"

#: The three routes this wave binds; only this one is checked here.
BOUND_BY_THIS_WAVE: tuple[str, ...] = ("git.pr", "merge.conflict", "transcript")

#: One Run row, so the projection is never empty by accident.
DOCUMENT: dict[str, Any] = {
    "run": {
        "RUN-00000010": {
            "urn": f"urn:eawf:{SCOPE}:run:RUN-00000010",
            "revision": 7,
            "status": "RUNNING",
        }
    }
}


def _event(sequence: int, **overrides: Any) -> RunEventRecord:
    """Return one event line at ``sequence``, a started reasoning turn by default."""
    fields: dict[str, Any] = {
        "event_ref": f"EVT-{sequence:08x}",
        "run_ref": RUN_URN,
        "run_sequence": sequence,
        "event_kind": RunEventKind.REASONING_STARTED,
        "provenance": "provider_native",
        "payload": ReasoningSummaryPayload(phase="started"),
        "actor": "OP-0001",
        "recorded_at": AT + timedelta(seconds=sequence),
    }
    return RunEventRecord.model_validate(fields | overrides)


def _summarized(sequence: int, summary: str = "read the loader and its tests") -> RunEventRecord:
    """Return the line that closes a reasoning turn, carrying the provider's own summary."""
    return _event(
        sequence,
        event_kind=RunEventKind.REASONING_SUMMARIZED,
        payload=ReasoningSummaryPayload(phase="summarized", summary=summary),
    )


def _command(
    sequence: int, *, kind: RunEventKind = RunEventKind.COMMAND_STARTED, **payload: Any
) -> RunEventRecord:
    """Return one command line at ``sequence``; the kind pins the payload's own phase."""
    phase = {
        RunEventKind.COMMAND_STARTED: "started",
        RunEventKind.COMMAND_OUTPUT: "output",
        RunEventKind.COMMAND_RESULT: "result",
    }[kind]
    fields: dict[str, Any] = {
        "command_family_ref": "shell",
        "command_ref": f"CMD-{sequence:08x}",
        "phase": phase,
        "execution": "foreground",
    }
    return _event(
        sequence, event_kind=kind, payload=CommandPayload.model_validate(fields | payload)
    )


def _gap(sequence: int, *, expected: int, observed: int, **payload: Any) -> RunEventRecord:
    """Return the gap line that explains sequences ``expected`` through ``observed - 1``."""
    fields: dict[str, Any] = {
        "expected_sequence": expected,
        "observed_sequence": observed,
        "replay_requested": True,
        "replay_capability_state": "verified",
        "gap_ref": f"GAP-{sequence:08x}",
    }
    return _event(
        sequence,
        event_kind=RunEventKind.EVENT_GAP,
        payload=EventGapPayload.model_validate(fields | payload),
    )


def _projection(
    route: str = TRANSCRIPT_ROUTE, *, cursor: int = 41208, document: Any = None
) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _view(events: Any = (), **kwargs: Any) -> TranscriptReadModel:
    """Return the transcript read model over the probe document."""
    return build_transcript_view(_projection(**kwargs), events=events)


def _fixture() -> Fixture:
    """Return the tracked prototype registers the epoch-1 mode renders from."""
    return load_fixture(FIXTURE_ROOT)


def _frame(model: TranscriptReadModel | None, *, width: int = 120, sel: int | None = None) -> str:
    """Return the transcript frame as one string, natively or in the epoch-1 mode."""
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.tr_sel = sel
    session.follow = sel is None
    return "\n".join(
        render_route(View(session=session, fixture=_fixture(), w=width, h=30, projection=model))
    )


def _app(**kwargs: Any) -> ConsoleApp:
    """Return a console bound to a seam already holding the transcript's projection."""
    seam = ProjectionSeam(route=TRANSCRIPT_ROUTE, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = _projection()
    app = ConsoleApp(_fixture(), FakeClock(), seam=seam, **kwargs)
    app.session.route = TRANSCRIPT_ROUTE
    return app


# ---------- the console composes the read model it draws ----------


def test_the_console_composes_the_transcript_read_model_from_the_seam() -> None:
    """The production call site: the app turns the held projection into the route's model."""
    model = _app().route_view()
    assert isinstance(model, TranscriptReadModel)
    assert model.route == TRANSCRIPT_ROUTE
    assert model.read_model is ROUTE_READ_MODELS[TRANSCRIPT_ROUTE]


def test_the_console_carries_its_run_events_into_the_transcript_model() -> None:
    """The event lines the console was given reach the blocks, and no other route's."""
    model = _app(run_events=(_event(1), _summarized(2))).route_view()
    assert isinstance(model, TranscriptReadModel)
    assert [block.sequence for block in model.blocks] == [1, 2]


def test_the_console_holds_no_transcript_model_for_another_route() -> None:
    """The seam carries one route; another route's frame falls back to epoch one."""
    app = _app()
    app.session.route = "unattended"
    assert app.route_view() is None


def test_the_route_has_both_projection_verbs() -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=TRANSCRIPT_ROUTE) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=TRANSCRIPT_ROUTE) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[TRANSCRIPT_ROUTE]
    assert set(TRANSCRIPT_FIELDS) == set(TRANSCRIPT_ROUTES)


# ---------- the order is the stream's ----------


def test_blocks_are_ordered_by_sequence_and_not_by_arrival() -> None:
    """A retried line arrives out of order; the transcript reads as the episode happened."""
    model = _view((_command(3), _event(1), _summarized(2)))
    assert [block.sequence for block in model.blocks] == [1, 2, 3]
    assert [block.kind for block in model.blocks] == [
        RunEventKind.REASONING_STARTED,
        RunEventKind.REASONING_SUMMARIZED,
        RunEventKind.COMMAND_STARTED,
    ]


def test_a_run_that_has_produced_nothing_draws_no_block() -> None:
    """The empty boundary: an absence of events is an absence, never an empty block."""
    model = _view(())
    assert model.blocks == ()
    assert model.purged == ()
    assert model.last_contiguous_sequence == 0
    assert transcript.NO_BLOCK in _frame(model)


def test_a_single_event_is_one_block_and_the_whole_contiguous_run() -> None:
    """The single boundary: one line reaches the frame and sets the cursor."""
    model = _view((_event(1),))
    assert [block.sequence for block in model.blocks] == [1]
    assert model.last_contiguous_sequence == 1
    assert "1 block ·" in _frame(model)


def test_a_quarantined_line_is_counted_and_drawn_by_nothing() -> None:
    """A line retained as a diagnostic is not part of the history."""
    late = _event(2, quarantine=QuarantineReason.LATE_AFTER_TERMINAL)
    model = _view((_event(1), late))
    assert [block.sequence for block in model.blocks] == [1]
    assert model.quarantined == 1
    assert "1 quarantined" in _frame(model)


def test_a_block_states_the_summary_the_provider_gave() -> None:
    """A summarized turn carries the provider's own text, never a synthesised one."""
    model = _view((_event(1), _summarized(2, "checked the acked cursor")))
    assert model.blocks[1].text.value == "checked the acked cursor"
    assert "checked the acked cursor" in _frame(model)


def test_a_started_turn_with_no_summary_yet_says_the_turn_is_open() -> None:
    """A started reasoning line has no text of its own; the block says what it is."""
    model = _view((_event(1),))
    assert model.blocks[0].text.value == OPEN_TURN_TEXT
    assert model.blocks[0].text.truth_kind is TruthKind.DERIVED


def test_a_command_block_names_its_family_execution_and_phase() -> None:
    """A detached background command in flight is a fact, so the block states it."""
    model = _view((_command(1, execution="background"),))
    assert model.blocks[0].text.value == "shell · background · started"
    assert "shell · background · started" in _frame(model)


def test_a_command_result_block_states_how_the_command_ended() -> None:
    """The result phase carries an outcome, and the block prints it beside the family."""
    result = _command(1, kind=RunEventKind.COMMAND_RESULT, exit_code=0, outcome="succeeded")
    model = _view((result,))
    assert model.blocks[0].text.value == "shell · foreground · result · succeeded"


def test_the_route_names_the_outcome_producer_it_waits_on() -> None:
    """A cell waiting on a named producer is a different answer from a blank cell."""
    model = _view((_event(1),))
    waiting = {spec.name: spec.missing_producer for spec in model.unproduced()}
    assert waiting == dict.fromkeys(("outcome", "cost"), RUN_OUTCOME_PRODUCER)
    assert RUN_OUTCOME_PRODUCER in _frame(model)


# ---------- a hole is drawn ----------


def test_a_recorded_gap_becomes_a_purged_block_in_its_own_position() -> None:
    """The transcript is never quietly shorter than the episode it claims to show."""
    model = _view((_event(1), _gap(2, expected=2, observed=5), _event(5)))
    assert [block.sequence for block in model.blocks] == [1, 2, 5]
    purged = model.blocks[1]
    assert purged.purged is not None
    assert (purged.purged.first, purged.purged.last) == (2, 4)
    assert purged.purged.count == 3


def test_a_purged_block_states_the_purged_truth_rather_than_the_unknown_one() -> None:
    """Unknown says nothing was learned; purged says the store holds none of the range."""
    model = _view((_event(1), _gap(2, expected=2, observed=5), _event(5)))
    field = model.blocks[1].text
    assert field.state is TruthState.PURGED
    assert field.value is None
    assert field.truth_kind is TruthKind.DERIVED
    assert field.producer_revision == BLOCK_REVISION
    assert field.missing_reason == PURGED_REASON.format(first=2, last=4)


def test_the_frame_marks_the_purged_range_and_counts_it() -> None:
    """An operator reads how much is missing, not merely that something is."""
    model = _view((_event(1), _gap(2, expected=2, observed=5), _event(5)))
    body = _frame(model)
    assert "3 sequences purged · 2-4" in body
    assert "1 purged range" in body
    assert "replay requested" in body


def test_a_gap_whose_replay_was_not_available_says_so() -> None:
    """A range nobody could ask for again is a different answer from one nobody asked for."""
    gap = _gap(
        2,
        expected=2,
        observed=4,
        replay_requested=False,
        replay_capability_state="unavailable",
    )
    assert "replay unavailable" in _frame(_view((_event(1), gap, _event(4))))


def test_a_gap_that_was_not_asked_for_says_that_instead() -> None:
    """The middle case: replay was available and the daemon did not ask."""
    gap = _gap(2, expected=2, observed=4, replay_requested=False)
    assert "no replay requested" in _frame(_view((_event(1), gap, _event(4))))


def test_the_one_sequence_gap_is_the_smallest_range_a_block_can_stand_for() -> None:
    """The off-by-one at the bottom of a range: a gap always covers at least one sequence."""
    model = _view((_event(1), _gap(2, expected=2, observed=3), _event(3)))
    covered = model.purged[0]
    assert (covered.first, covered.last, covered.count) == (2, 2, 1)


def test_a_gap_naming_no_missing_sequence_is_refused_upstream() -> None:
    """A gap that describes no hole is not a gap, and the record itself refuses it."""
    with pytest.raises(ValidationError, match="a gap runs from"):
        _gap(2, expected=4, observed=4)


def test_the_contiguous_run_stops_where_the_hole_starts() -> None:
    """Later events stay readable; they are no longer a history with nothing missing."""
    model = _view((_event(1), _gap(2, expected=2, observed=5), _event(5)))
    assert model.last_contiguous_sequence == 1
    assert model.derivation_stopped is True
    assert "contiguous through 1" in _frame(model)


def test_an_unexplained_hole_is_refused_rather_than_folded_over() -> None:
    """A lost line means a cursor that skips it, so the read fails instead of reporting one."""
    with pytest.raises(ValueError, match="not contiguous"):
        _view((_event(1), _event(3)))


# ---------- the thinking state is derived and says so ----------


def test_an_open_reasoning_turn_makes_the_transcript_thinking() -> None:
    """The state is folded out of a turn that opened and has not been summarized."""
    model = _view((_event(1),))
    assert model.is_thinking()
    assert model.thinking.value == THINKING
    assert model.thinking.truth_kind is TruthKind.DERIVED


def test_a_summarized_turn_closes_the_thinking_state() -> None:
    """The pairing is what the derivation rests on, so the close ends it."""
    model = _view((_event(1), _summarized(2)))
    assert not model.is_thinking()
    assert model.thinking.state is TruthState.UNKNOWN
    assert model.thinking.missing_reason == NO_OPEN_TURN_REASON


def test_a_second_turn_opened_after_a_close_is_open_again() -> None:
    """The walk is a fold, not a first-match: the newest pairing decides."""
    model = _view((_event(1), _summarized(2), _event(3)))
    assert model.is_thinking()
    assert open_reasoning_turn([_event(1), _summarized(2), _event(3)]) is not None


def test_a_run_with_no_reasoning_at_all_is_not_thinking() -> None:
    """The empty boundary of the derivation: no turn was ever opened."""
    model = _view((_command(1),))
    assert not model.is_thinking()
    assert open_reasoning_turn([_command(1)]) is None


def test_a_quarantined_open_turn_neither_opens_nor_closes_one() -> None:
    """A repudiated line derives nothing, including the state of a thought."""
    late = _event(2, quarantine=QuarantineReason.SEQUENCE_CONFLICT)
    assert open_reasoning_turn([_event(1), _summarized(1), late]) is None


def test_the_frame_labels_the_thinking_state_as_derived() -> None:
    """A frame that presented the inference as an observation would be lying about it."""
    body = _frame(_view((_event(1),)))
    assert f"{THINKING} · {transcript.DERIVED_LABEL}" in body


def test_the_frame_draws_the_unknown_token_when_no_turn_is_open() -> None:
    """An absent state is stated, and still labelled derived."""
    body = _frame(_view((_event(1), _summarized(2))))
    assert f"? · {transcript.DERIVED_LABEL}" in body


# ---------- the frame, the cursor and the epoch-1 mode ----------


def test_the_frame_is_exactly_the_terminal_height() -> None:
    """A native frame is laid out to the same H rows as every other console frame."""
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    rows = render_route(
        View(session=session, fixture=_fixture(), w=120, h=30, projection=_view((_event(1),)))
    )
    assert len(rows) == 30


def test_the_cursor_follows_the_tail_of_the_run() -> None:
    """A following transcript sits on the newest block, whatever arrived before it."""
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.follow = True
    render_route(
        View(
            session=session,
            fixture=_fixture(),
            w=120,
            h=30,
            projection=_view((_event(1), _summarized(2), _command(3))),
        )
    )
    assert session.tr_sel == 2
    assert session.count == 3


def test_a_cursor_past_the_last_block_is_clamped_onto_it() -> None:
    """A cursor kept across a shorter run lands on a block, never past the end."""
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.tr_sel = 99
    render_route(
        View(session=session, fixture=_fixture(), w=120, h=30, projection=_view((_event(1),)))
    )
    assert session.tr_sel == 0


def test_a_block_at_an_offset_no_block_holds_is_none() -> None:
    """The two ends of the run: below zero and past the last are both absent."""
    model = _view((_event(1), _summarized(2)))
    assert model.block_at(-1) is None
    assert model.block_at(2) is None
    assert model.block_at(1) is model.blocks[1]


def test_the_epoch_one_frame_still_renders_when_no_read_model_is_held() -> None:
    """The prototype mode is the tracked golden contract; binding must not cost it."""
    body = _frame(None)
    assert "Transcript" in body
    assert "UNSTATED" not in body
    assert "STATE  " not in body


def test_a_read_model_for_another_route_is_not_adopted() -> None:
    """A frame drawn from another route's rows would show one route's records as another's."""
    model = _view((_event(1),))
    session = Session()
    session.route = "unattended"
    view = View(session=session, fixture=_fixture(), w=120, h=30, projection=model)
    assert native(view) is None


def test_the_transcript_route_has_no_operations_read_model() -> None:
    """A projection of another family is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no operations read model"):
        build_operations_view(_projection())


def test_an_operations_route_has_no_transcript_read_model() -> None:
    """The refusal runs both ways; neither family answers for the other."""
    with pytest.raises(ValueError, match="has no transcript read model"):
        build_transcript_view(_projection("crash.recovery"))


def test_a_payload_the_transcript_has_no_sentence_for_states_why() -> None:
    """A line the console holds and cannot read aloud says so rather than drawing a blank.

    A gap payload is answered by the purged cell in the block run, so reaching the text
    helper with one is how the fallback every later payload kind will land in is proved
    to say something rather than crash the frame.
    """
    field = block_text(_gap(2, expected=2, observed=4))
    assert field.state is TruthState.UNKNOWN
    assert field.value is None
    assert field.missing_reason == NO_TEXT_REASON


def test_an_event_of_another_run_is_still_folded_when_it_is_handed_in() -> None:
    """The model folds what it was given; selecting one Run's lines is the caller's job."""
    model = _view((_event(1, run_ref=OTHER_RUN),))
    assert [block.sequence for block in model.blocks] == [1]


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    with pytest.raises(KeyError):
        _view((_event(1),)).rows[0].field("tokens")


def test_an_empty_register_counts_zero_and_draws_no_row() -> None:
    """A register the route binds and that holds nothing counts zero, honestly."""
    model = build_transcript_view(_projection(document={}))
    assert model.counts == {"run": 0}
    assert model.rows == ()


# ---------- the coverage grid no longer lists these three as holes ----------


@pytest.mark.parametrize("route", BOUND_BY_THIS_WAVE)
def test_this_waves_routes_are_listed_bound_and_served(route: str) -> None:
    """The three routes this wave binds moved out of the hole list in the same commit."""
    row = next(r for r in load_manifest().routes if r.route == route)
    assert row.binding == "bound"
    assert row.bound_by is None
    assert REGISTRY.by_id[route].key in ROUTE_COLLECTIONS
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS


def test_the_remaining_holes_are_the_ones_later_waves_still_owe() -> None:
    """The grid stays total: binding these routes leaves exactly the other holes."""
    holes = sorted(row.route for row in load_manifest().routes if row.binding == "hole")
    assert holes == ["settings", "settings.stack"]
    assert not set(holes) & set(BOUND_BY_THIS_WAVE)


def test_the_grid_and_the_console_still_agree_in_both_directions() -> None:
    """A route bound here and left a hole in the grid is the drift the grid exists to catch."""
    assert coverage_defects(load_manifest()) == ()
