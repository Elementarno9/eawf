"""The transcript draws a Run's ordered event log, its holes, and its thinking state.

One route leaves the prototype registers here, and three claims about it are worth a test
each.

*The order is the stream's, not the file's.* Event lines arrive in ledger order, which is
arrival order, and a retried line arrives out of sequence. The blocks are sorted by
``run_sequence`` so the transcript reads as the episode happened, and quarantined lines --
retained as diagnostics and derived from by nothing -- never appear in the run at all.

*A hole is drawn.* A recorded gap covers a range the daemon never received. The range
becomes a block of its own saying how many sequences are missing, in the position it
occupies, so the transcript is never quietly shorter than the episode it claims to show.

*The thinking state says how it is known.* What exists is a reasoning turn that opened
and has not been summarized. A provider's own start marker states the turn, so the state
is observed; where eawf inferred the opening for a provider with no marker, the state is
a derived truth field and the frame prints the label beside it, so it never presents the
inference as an observation.

The last group is the one the coverage grid owes. ``transcript`` is out of the manifest's
hole list in the same commit that binds it, and this suite checks the grid and the console
agree in both directions rather than taking the grid's word for it.

CON-169, CON-170, CON-171 and CON-172 are held in the last section: the clock and the
kind in full, the fold marks, the header derived from the blocks, the tail and the
follow, the held clock, and colour on the kind cell alone.
"""

from __future__ import annotations

import asyncio
import re
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
    RUN_NOT_ENDED,
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
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route, transcript
from eawf.surfaces.tui.console.renderers.read_model import native
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import journey_support as js
from tests.tui.surfaces.tui.console.test_console_verbs import _Host
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


def test_run_062_the_console_reads_the_runs_lines_through_the_seam() -> None:
    """RUN-062, UI-072: the lines the transcript draws are read from the daemon for the Run
    the route is about, once its row names the URN they are filed under."""
    daemon = js.DocumentDaemon(DOCUMENT)
    daemon.run_events = {DOCUMENT["run"]["RUN-00000010"]["urn"]: (_event(1), _summarized(2))}
    seam = ProjectionSeam(
        route=TRANSCRIPT_ROUTE,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
    )
    app = ConsoleApp(_fixture(), FakeClock(), seam=seam)
    app.session.route = TRANSCRIPT_ROUTE
    seam.retarget(TRANSCRIPT_ROUTE)
    seam.about("RUN-00000010")
    loaded = asyncio.run(seam.sync())
    assert loaded.index(TRANSCRIPT_ROUTE) < loaded.index(RUN_EVENTS_READ_METHOD)
    model = app.route_view()
    assert isinstance(model, TranscriptReadModel)
    assert [block.sequence for block in model.blocks] == [1, 2]


def test_a_transcript_whose_lines_were_not_read_draws_no_block() -> None:
    """No fixture stands in for a Run whose lines the seam has not read."""
    model = _app().route_view()
    assert isinstance(model, TranscriptReadModel)
    assert model.blocks == ()


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


def test_a_running_runs_outcome_says_it_has_not_ended() -> None:
    """A Run still running states no outcome, and says so rather than blaming a producer."""
    model = _view((_event(1),))
    assert model.unproduced() == ()
    assert RUN_NOT_ENDED in _frame(model)


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


# ---------- the thinking state says whether it was observed or derived ----------


def _inferred(sequence: int) -> RunEventRecord:
    """Return a reasoning start eawf inferred for a provider that marks none."""
    return _event(sequence, provenance="eawf_derived")


def test_an_open_reasoning_turn_makes_the_transcript_thinking() -> None:
    """The state is folded out of a turn the provider's own start marker opened."""
    model = _view((_event(1),))
    assert model.is_thinking()
    assert model.thinking.value == THINKING
    assert model.thinking.truth_kind is TruthKind.OBSERVED


def test_an_inferred_reasoning_start_makes_the_thinking_state_derived() -> None:
    """With no start marker from the provider, the open turn is an inference."""
    model = _view((_inferred(1),))
    assert model.is_thinking()
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


def test_the_frame_labels_an_inferred_thinking_state_as_derived() -> None:
    """A frame that presented the inference as an observation would be lying about it."""
    body = _frame(_view((_inferred(1),)))
    assert f"{THINKING} · {transcript.DERIVED_LABEL}" in body


def test_the_frame_states_an_observed_thinking_state_without_the_label() -> None:
    """A turn the provider marked is not an inference, so it carries no derived label."""
    body = _frame(_view((_event(1),)))
    state = next(line for line in body.split("\n") if line.startswith(" STATE"))
    assert THINKING in state
    assert transcript.DERIVED_LABEL not in state


def test_the_frame_draws_the_unknown_token_when_no_turn_is_open() -> None:
    """An absent state is stated as its token and word, and still labelled derived."""
    body = _frame(_view((_event(1), _summarized(2))))
    state = next(line for line in body.split("\n") if line.startswith(" STATE"))
    assert state.startswith(f" STATE     ? unknown · {transcript.DERIVED_LABEL} · ")


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


def test_no_route_is_left_owed_to_a_later_wave() -> None:
    """The grid stays total: binding these routes leaves nothing waiting on a wave."""
    holes = sorted(row.route for row in load_manifest().routes if row.binding == "hole")
    assert holes == []


def test_the_grid_and_the_console_still_agree_in_both_directions() -> None:
    """A route bound here and left a hole in the grid is the drift the grid exists to catch."""
    assert coverage_defects(load_manifest()) == ()


# ---------- CON-169 to CON-172: the route draws the run's own words ----------

_BLOCK_ROW = re.compile(r"^[ ▸](?P<clock>\d\d:\d\d:\d\d)  (?P<glyph>\S) (?P<word>[a-z]+)\s")
_SHELL_TYPICAL = 30


def _at(seconds: int) -> datetime:
    return AT + timedelta(seconds=seconds)


def _live_run() -> tuple[RunEventRecord, ...]:
    """Return a Run thinking for 18s, with one foreground and one background command going.

    A finished shell command took 30s, so shell's typical duration is derived as 30s.
    """
    output = CommandPayload(
        command_family_ref="shell",
        command_ref="CMD-000000f9",
        phase="output",
        execution="foreground",
        stream="stdout",
        chunk_ref="artifact://runs/chunk-0001",
    )
    lines = (
        (_command(1, command_ref="CMD-0000d0e0"), 0),
        (
            _command(
                2,
                kind=RunEventKind.COMMAND_RESULT,
                command_ref="CMD-0000d0e0",
                exit_code=0,
                outcome="succeeded",
            ),
            _SHELL_TYPICAL,
        ),
        (_command(3, command_ref="CMD-000000b9", execution="background"), 40),
        (_event(4), 42),
        (_command(5, command_ref="CMD-000000f9"), 48),
        (_event(6, event_kind=RunEventKind.COMMAND_OUTPUT, payload=output), 60),
    )
    return tuple(line.model_copy(update={"recorded_at": _at(at)}) for line, at in lines)


def _rows(frame: str) -> list[str]:
    return frame.split("\n")


def _blocks(frame: str) -> list[re.Match[str]]:
    return [m for row in _rows(frame) if (m := _BLOCK_ROW.match(row))]


def test_con_169_each_block_is_its_clock_then_its_kind_in_full() -> None:
    model = _view(_live_run())
    blocks = _blocks(_frame(model))
    assert len(blocks) == len(model.blocks)
    for found in blocks:
        assert found.group("glyph") == transcript.NATIVE_GLYPH[found.group("word")]
    words = [found.group("word") for found in blocks]
    assert words == ["tool", "tool", "background", "thinking", "running", "tool"]


def test_con_169_the_header_states_what_is_true_now_derived_from_the_blocks() -> None:
    header = _rows(_frame(_view(_live_run())))[1]
    assert header.startswith(
        " Run RUN-00000010 · THINKING for 18s · 1 running in the background · following · 6 blocks"
    )


def test_con_169_a_block_in_flight_states_its_elapsed_and_the_typical_time_where_width_allows() -> (
    None
):
    wide = next(r for r in _rows(_frame(_view(_live_run()), width=120)) if "⋯ running" in r)
    narrow = next(r for r in _rows(_frame(_view(_live_run()), width=80)) if "⋯ running" in r)
    widest = next(r for r in _rows(_frame(_view(_live_run()), width=160)) if "⋯ running" in r)
    assert wide.rstrip().endswith(f"12s · ~{_SHELL_TYPICAL}s")
    assert narrow.rstrip().endswith("12s")
    assert widest.rstrip().endswith(f"12s · ~{_SHELL_TYPICAL}s typical")
    for frame in (wide, narrow, widest):
        assert "estimate" not in frame and "left" not in frame and "remaining" not in frame


def test_con_169_a_purged_range_shows_purged_in_its_fold_slot() -> None:
    frame = _frame(_view((_event(1), _gap(2, expected=2, observed=5), _summarized(5))))
    purged = next(r for r in _rows(frame) if "✗ purged" in r)
    assert _BLOCK_ROW.match(purged) is not None
    assert purged.rstrip().endswith(transcript.PURGED_MARK)


def _long_summary() -> tuple[RunEventRecord, ...]:
    words = " ".join(f"word{n}" for n in range(60))
    return (_event(1), _summarized(2, words))


def test_con_169_a_long_block_folds_beyond_its_preview_and_states_how_much() -> None:
    model = _view(_long_summary())
    frame = _frame(model, width=80, sel=1)
    head = next(r for r in _rows(frame) if r.startswith("▸"))
    found = re.search(r"▸ (\d+) lines\s*$", head)
    assert found is not None, head
    hidden = int(found.group(1))
    assert hidden > 0
    assert "…" not in "".join(r for r in _rows(frame) if "word" in r)


def test_con_169_enter_unfolds_the_block_in_place_and_shows_every_line() -> None:
    model = _view(_long_summary())
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.tr_sel = 1
    session.follow = False
    view = View(session=session, fixture=_fixture(), w=80, h=30, projection=model)
    closed = render_route(view)
    ctx = Ctx(session=session, fixture=_fixture(), host=_Host(), w=80, h=30, projection=model)
    assert transcript.seam(ctx, "Enter", False)
    opened = render_route(view)
    head = next(r for r in opened if r.startswith("▸"))
    assert re.search(r"▾ \d+ lines\s*$", head)
    text = "\n".join(opened)
    assert "word59" in text and "word59" not in "\n".join(closed)


def test_con_170_blocks_run_in_stream_order_and_open_on_the_latest() -> None:
    model = _view(_live_run())
    frame = _frame(model)
    clocks = [found.group("clock") for found in _blocks(frame)]
    assert clocks == sorted(clocks)
    caret = [r for r in _rows(frame) if r.startswith("▸")]
    assert len(caret) == 1 and "12:01:00" in caret[0]


def test_con_170_a_short_run_fills_from_the_bottom_of_its_section() -> None:
    rows = _rows(_frame(_view((_event(1),))))
    block = next(i for i, r in enumerate(rows) if _BLOCK_ROW.match(r))
    closing = next(i for i, r in enumerate(rows) if i > block and r.startswith("─"))
    assert closing == block + 1
    assert rows[block - 1].strip() == ""


def test_con_171_a_thinking_block_states_its_state_and_elapsed_never_its_content() -> None:
    frame = _frame(_view(_live_run()))
    thinking = next(r for r in _rows(frame) if "° thinking" in r)
    assert OPEN_TURN_TEXT in thinking
    assert thinking.rstrip().endswith("18s")


def test_con_171_a_long_transcript_shows_its_tail_and_counts_the_rest() -> None:
    events = tuple(_event(n) if n % 2 else _summarized(n) for n in range(1, 61))
    frame = _frame(_view(events))
    assert re.search(r"^ … \d+ above", frame, re.MULTILINE)
    assert _blocks(frame)[-1].group("clock") == "12:01:00"


def test_con_172_a_block_that_lands_while_following_takes_the_cursor() -> None:
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.follow = True
    view = View(session=session, fixture=_fixture(), w=120, h=30, projection=_view(_live_run()[:3]))
    render_route(view)
    assert session.tr_sel == 2
    grown = View(session=session, fixture=_fixture(), w=120, h=30, projection=_view(_live_run()))
    render_route(grown)
    assert session.tr_sel == 5


def test_con_172_a_held_view_keeps_its_row_and_counts_what_arrived_below() -> None:
    events = tuple(_event(n) if n % 2 else _summarized(n) for n in range(1, 61))
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.follow = False
    session.tr_sel = 3
    frame = "\n".join(
        render_route(
            View(session=session, fixture=_fixture(), w=120, h=30, projection=_view(events))
        )
    )
    assert session.tr_sel == 3
    assert re.search(r"^ … \d+ below", frame, re.MULTILINE)


def test_con_172_f_holds_and_follows_the_tail() -> None:
    model = _view(_live_run())
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.follow = True
    ctx = Ctx(session=session, fixture=_fixture(), host=_Host(), w=120, h=30, projection=model)
    transcript.seam(ctx, "ArrowUp", False)
    assert session.follow is False
    transcript.seam(ctx, "f", False)
    assert session.follow is True and session.tr_sel == len(model.blocks) - 1
    header = _rows(_frame(model))[1]
    assert "· following ·" in header


def test_con_172_a_held_clock_freezes_the_feed_on_the_authored_tail() -> None:
    model = _view(_live_run())

    def render(now: datetime | None, held: bool) -> str:
        session = Session()
        session.route = TRANSCRIPT_ROUTE
        session.follow = True
        return "\n".join(
            render_route(
                View(
                    session=session,
                    fixture=_fixture(),
                    w=120,
                    h=30,
                    projection=model,
                    now=now,
                    held=held,
                )
            )
        )

    held_early = render(_at(61), held=True)
    held_late = render(_at(600), held=True)
    live_late = render(_at(600), held=False)
    assert held_early == held_late
    assert "THINKING for 18s" in held_late
    assert "THINKING for 9m 18s" in live_late


def test_con_172_colour_sits_on_the_kind_cell_only() -> None:
    frame = _frame(_view(_live_run()))
    for row in (r for r in _rows(frame) if _BLOCK_ROW.match(r)):
        found = _BLOCK_ROW.match(row)
        assert found is not None
        # a quality marker keeps its own mark; the kind colour is the one unmarked run
        strokes = paint(row, Part.BODY)
        coloured = [s for s in strokes if s.surface not in (None, "caret") and s.mark is None]
        assert len(coloured) == 1
        assert coloured[0].text == f"{found.group('glyph')} {found.group('word')}"


def test_con_169_the_keybar_walks_blocks_and_folds_them() -> None:
    bar = _rows(_frame(_view(_long_summary()), width=80, sel=1))[-1].split()
    assert bar[:4] == ["↑↓", "block", "Enter", "fold"]
    assert bar[4:6] == ["f", "follow"]


def test_enter_fold_is_left_off_a_block_that_folds_nothing() -> None:
    bar = _rows(_frame(_view(_live_run())))[-1].split()
    assert bar[:4] == ["↑↓", "block", "f", "follow"]


def test_con_172_the_scrollbar_column_is_drawn_only_when_something_is_hidden() -> None:
    short = _rows(_frame(_view((_event(1),))))
    assert not any(r.endswith("█") for r in short)
    events = tuple(_event(n) if n % 2 else _summarized(n) for n in range(1, 61))
    long = _rows(_frame(_view(events)))
    assert any(r.endswith("█") for r in long)


def test_con_169_the_epoch_one_feed_renders_a_waiting_question_and_its_estimate_as_typical() -> (
    None
):
    frame = _frame(None)
    question = next(r for r in _rows(frame) if "? question" in r)
    assert re.search(r"waiting \d+m \d\ds", question)
    assert "estimate" not in frame


# ---------- an empty transcript answers truthfully ----------


@pytest.mark.parametrize(("key", "verb"), [("y", "copy"), ("Enter", "fold")])
def test_a_transcript_with_no_block_names_that_nothing_is_there(key: str, verb: str) -> None:
    model = _view()
    assert model.blocks == ()
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    copied: list[str] = []

    def clipboard(text: str) -> bool:
        copied.append(text)
        return True

    ctx = Ctx(
        session=session,
        fixture=_fixture(),
        host=_Host(),
        w=80,
        h=30,
        projection=model,
        clipboard=clipboard,
    )
    assert transcript.seam(ctx, key, False)
    toast = session.toasts[-1].text
    assert toast == f"no block to {verb} · this Run has produced no event"
    assert "block 1" not in toast
    assert copied == []
    view = View(session=session, fixture=_fixture(), w=80, h=30, projection=model)
    bar = render_route(view)[-1]
    assert " y copy" not in f" {bar}"


def test_y_on_a_held_block_puts_the_block_text_on_the_clipboard() -> None:
    model = _view(_long_summary())
    session = Session()
    session.route = TRANSCRIPT_ROUTE
    session.tr_sel = 1
    copied: list[str] = []

    def clipboard(text: str) -> bool:
        copied.append(text)
        return True

    ctx = Ctx(
        session=session,
        fixture=_fixture(),
        host=_Host(),
        w=80,
        h=30,
        projection=model,
        clipboard=clipboard,
    )
    assert transcript.seam(ctx, "y", False)
    block = model.block_at(1)
    assert block is not None
    assert copied == [block.text.value]
    assert session.toasts[-1].title == "copied"
