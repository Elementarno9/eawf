"""Attention with no producer, a pause that arrives as a patch, and the budget reading.

Three claims meet on the Attention and budget routes.

The first is the one the console is most likely to get wrong. The pending-action producer
has not shipped, so the Attention register is bound and unwritten, and a bound unwritten
register is not an empty one: ``0 actions`` would tell an operator the queue is clear when
in truth nothing has ever been asked. The count is withheld and the unknown truth token
stands in its place, naming why.

The second is the transport. A pause used to reach the console as whole state over the
socket binding, and a console that re-reads the world on every pause is a console that
cannot say which cursor it stands at. A committed pending-action transition now produces a
keyed patch addressed to the Attention route, the seam applies it through its own sink,
and applying it opens no overlay and moves no focus: a patch is a row arriving, not an
operator being interrupted.

The third is the budget. Cost ceiling and Notifications read the notice's own contract --
which control a termination opens, which status it leaves, and whether the crossing
interrupts anybody -- rather than restating those as frame literals. Which Runs a cap
stopped is a run-ledger fact, so it comes back unknown naming that, and neither route
promotes a terminal status into a reason a Run ended.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    KeyedPatch,
    RouteProjection,
    build_route_projection,
    patches_for_event,
)
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    BUDGET_UNSTATED_REASON,
    COST_CEILING_ROUTE,
    NOTIFICATIONS_ROUTE,
    UNWRITTEN_COLLECTIONS,
    UNWRITTEN_REASON,
    RegisterView,
    attention_mine,
    budget_reading,
    build_register_view,
    notice_interrupts,
)
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.budget_notice import (
    BUDGET_TERMINATION_CONTROL,
    BUDGET_TERMINATION_STATUS,
    BudgetNotice,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.registers import UNWRITTEN_ROW
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import truth_cell

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"

#: A pending action's qualified address, in the epoch-2 URN grammar the patch builder
#: parses. The repository slot is real because a pending action is repository-level.
ACTION_URN = "eawf://WSP-A/EAWF/REP-A/pending-action/ACT-0001"

#: Two Runs, one of them in the status a confirmed budget termination leaves, so a route
#: tempted to call that Run budget-stopped has something to be tempted by.
DOCUMENT: dict[str, Any] = {
    "run": {
        "RUN-0000000a": {"urn": f"{SCOPE}/run/RUN-0000000a", "revision": 1, "status": "RUNNING"},
        "RUN-0000000d": {
            "urn": f"{SCOPE}/run/RUN-0000000d",
            "revision": 5,
            "status": BUDGET_TERMINATION_STATUS.value,
        },
    },
    "pending_action": {
        "ACT-0001": {"urn": ACTION_URN, "revision": 1, "status": "OPEN"},
    },
}


def _projection(route: str, *, cursor: int = 41208, document: Any = None) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _register(route: str, **kwargs: Any) -> RegisterView:
    """Return the register read model of ``route`` over the probe document."""
    return build_register_view(_projection(route, **kwargs))


def _frame(register: RegisterView) -> tuple[list[str], Session]:
    """Return the native frame ``register`` renders, and the session it published into."""
    session = Session()
    session.route = register.route
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=120,
        h=24,
        register=register,
        attention=register if register.route == ATTENTION_ROUTE else None,
    )
    return render_route(view), session


def _pause_envelope(*, sequence: int = 41209, status: str = "OPEN") -> Envelope:
    """Return the committed transition a raised needs_user pause records."""
    return Envelope(
        id=f"evt-{sequence:04d}",
        kind=StoreKind.EVENT,
        scope_id=SCOPE,
        created_at=AT,
        summary="needs_user pause raised",
        payload={
            "entity_ref": ACTION_URN,
            "to_status": status,
            "revision_after": 1,
            "canonical_sequence": sequence,
        },
    )


def _seam_holding(projection: RouteProjection) -> ProjectionSeam:
    """Return a seam bound to no tree, already holding ``projection``."""
    seam = ProjectionSeam(route=projection.route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = projection
    return seam


# ---------- the attention register is bound and unwritten ----------


def test_the_attention_route_binds_the_pending_action_register() -> None:
    """The route reads one register, and it is the one a pending action lives in."""
    assert ROUTE_COLLECTIONS[ATTENTION_ROUTE] == (Epoch2Collection.PENDING_ACTION,)
    assert Epoch2Collection.PENDING_ACTION in UNWRITTEN_COLLECTIONS


def test_the_unwritten_register_states_no_count_rather_than_zero() -> None:
    """A register with no producer is withheld; a zero would be a count nobody took."""
    register = _register(ATTENTION_ROUTE)
    assert register.withheld == ("pending_action",)
    assert register.counts == {}
    assert register.count("pending_action") is None


def test_a_document_row_of_an_unwritten_register_is_not_drawn_as_a_count() -> None:
    """Even a row left in the document does not turn a withheld register into a number."""
    register = _register(ATTENTION_ROUTE)
    assert register.rows == ()
    assert attention_mine(register).state is TruthState.UNKNOWN


def test_the_attention_frame_shows_the_unknown_token_for_action_rows() -> None:
    """The frame prints the truth token where the action count would be, and says why."""
    rows, _session = _frame(_register(ATTENTION_ROUTE))
    mine = next(row for row in rows if row.startswith(" MINE"))
    assert truth_cell("unknown") in mine
    assert "0" not in mine.split("·")[0]
    assert any(UNWRITTEN_REASON in row for row in rows)


def test_the_attention_frame_names_the_register_nothing_writes() -> None:
    """The withheld register is named on the frame rather than quietly missing."""
    rows, _session = _frame(_register(ATTENTION_ROUTE))
    unwritten = next(row for row in rows if row.startswith(UNWRITTEN_ROW))
    assert "pending_action" in unwritten
    assert truth_cell("unknown") in unwritten


def test_an_empty_attention_document_reads_the_same_as_a_populated_one() -> None:
    """Withholding is a property of the producer, not of what the document happens to hold."""
    assert _register(ATTENTION_ROUTE, document={}).withheld == ("pending_action",)


# ---------- a needs_user pause arrives as a keyed patch ----------


def test_a_pause_transition_produces_a_keyed_patch_for_the_attention_route() -> None:
    """The pause rides the projection feed rather than a whole-state refresh."""
    patches = patches_for_event(_pause_envelope())
    addressed = [p for p in patches if ATTENTION_ROUTE in p.routes]
    assert len(addressed) == 1
    patch = addressed[0]
    assert patch.projection_kind is ReadModelKind.ATTENTION_PAGE
    assert patch.canonical_sequence == 41209
    assert patch.entries[0].key == "ACT-0001"
    assert patch.entries[0].collection is Epoch2Collection.PENDING_ACTION


def test_the_patch_carries_only_the_three_columns_the_entry_shape_states() -> None:
    """The entry shape is untouched: urn, revision and status, and nothing per route."""
    entry = patches_for_event(_pause_envelope())[0].entries[0]
    assert set(entry.model_dump()) == {"key", "urn", "collection", "revision", "status"}


def test_applying_the_pause_patch_opens_no_overlay_and_moves_no_focus() -> None:
    """A row arriving is not an operator being interrupted."""
    seam = _seam_holding(_projection(ATTENTION_ROUTE, document={}))
    session = Session()
    session.route = ATTENTION_ROUTE
    session.sel, session.sel_id, session.overlay = 0, None, None

    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    asyncio.run(seam.apply_patch(patch))

    assert session.overlay is None
    assert session.sel == 0
    assert seam.projection is not None
    assert [row.key for row in seam.projection.rows] == ["ACT-0001"]
    assert seam.projection.header.source_cursor == "41209"


def test_the_applied_patch_still_leaves_the_register_withheld() -> None:
    """One arriving row does not make a register written; the producer does."""
    seam = _seam_holding(_projection(ATTENTION_ROUTE, document={}))
    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    asyncio.run(seam.apply_patch(patch))
    assert seam.projection is not None
    register = build_register_view(seam.projection)
    assert register.withheld == ("pending_action",)
    assert attention_mine(register).missing_reason == UNWRITTEN_REASON


def test_a_patch_for_another_route_does_not_reach_the_attention_seam() -> None:
    """One feed carries every route; a patch that is not ours changes nothing."""
    seam = _seam_holding(_projection(ATTENTION_ROUTE, document={}))
    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    elsewhere = patch.model_copy(update={"routes": ("backlog",)})
    asyncio.run(seam.apply_patch(elsewhere))
    assert seam.projection is not None
    assert seam.projection.rows == ()


def test_a_pause_stating_no_ordinal_produces_no_patch_at_all() -> None:
    """An envelope with no committed ordinal cannot be ordered, so it patches nothing."""
    envelope = _pause_envelope().model_copy(
        update={"payload": {"entity_ref": ACTION_URN, "to_status": "OPEN", "revision_after": 1}}
    )
    assert patches_for_event(envelope) == ()


def test_a_pause_stating_no_status_is_refused_rather_than_patched_blank() -> None:
    """A patch that could not say what the row became would be worse than none."""
    envelope = _pause_envelope().model_copy(
        update={
            "payload": {
                "entity_ref": ACTION_URN,
                "revision_after": 1,
                "canonical_sequence": 41209,
            }
        }
    )
    with pytest.raises(ValueError, match="states no to_status"):
        patches_for_event(envelope)


def test_a_patch_the_seam_never_loaded_is_ignored() -> None:
    """A patch arriving before the first read has no rows to replace."""
    seam = ProjectionSeam(route=ATTENTION_ROUTE, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    asyncio.run(seam.apply_patch(patch))
    assert seam.projection is None


def test_the_replayed_patch_and_a_clean_read_digest_alike() -> None:
    """The patched projection is the projection, not a second shape beside it."""
    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    seam = _seam_holding(_projection(ATTENTION_ROUTE, document={}))
    asyncio.run(seam.apply_patch(patch))
    clean = _projection(
        ATTENTION_ROUTE,
        cursor=41209,
        document={
            "pending_action": {"ACT-0001": {"urn": ACTION_URN, "revision": 1, "status": "OPEN"}}
        },
    )
    assert seam.projection is not None
    assert seam.projection.digest == clean.digest


def test_a_keyed_patch_is_what_the_projection_feed_carries() -> None:
    """The pause reaches the console as the feed's own shape, validated as one."""
    patch = next(p for p in patches_for_event(_pause_envelope()) if ATTENTION_ROUTE in p.routes)
    assert KeyedPatch.model_validate(patch.model_dump(mode="json")) == patch


# ---------- cost.ceiling and notifications read the budget event ----------


@pytest.mark.parametrize("route", [COST_CEILING_ROUTE, NOTIFICATIONS_ROUTE])
def test_both_budget_routes_read_the_notice_contract(route: str) -> None:
    """The control, the status and the interrupt answer all come off the notice."""
    reading = budget_reading(_register(route))
    assert reading.control == BUDGET_TERMINATION_CONTROL.value
    assert reading.terminal_status == BUDGET_TERMINATION_STATUS.value
    assert reading.interrupts is False


def test_the_interrupt_answer_is_read_off_the_notice_rather_than_declared() -> None:
    """A notice reports a reading and waits on no answer, and that is where it is stated."""
    assert notice_interrupts() is False
    assert BudgetNotice.model_fields["blocking"].get_default() is False


@pytest.mark.parametrize("route", [COST_CEILING_ROUTE, NOTIFICATIONS_ROUTE])
def test_the_stopped_runs_are_unknown_rather_than_guessed_from_a_status(route: str) -> None:
    """A Run in the terminal status is not thereby a Run a cap stopped."""
    reading = budget_reading(_register(route))
    assert reading.stopped.state is TruthState.UNKNOWN
    assert reading.stopped.value is None
    assert reading.stopped.missing_reason == BUDGET_UNSTATED_REASON


@pytest.mark.parametrize("route", [COST_CEILING_ROUTE, NOTIFICATIONS_ROUTE])
def test_the_budget_frame_prints_the_reading_it_read(route: str) -> None:
    """Every part of the reading reaches the frame, the unknown token included."""
    rows, _session = _frame(_register(route))
    budget = next(row for row in rows if row.startswith(" BUDGET"))
    stopped = next(row for row in rows if row.startswith(" STOPPED"))
    assert BUDGET_TERMINATION_CONTROL.value in budget
    assert BUDGET_TERMINATION_STATUS.value in budget
    assert any("does not interrupt you" in row for row in rows)
    assert truth_cell("unknown") in stopped
    assert BUDGET_UNSTATED_REASON in stopped


@pytest.mark.parametrize("route", [COST_CEILING_ROUTE, NOTIFICATIONS_ROUTE])
def test_both_budget_routes_count_the_run_register_they_bind(route: str) -> None:
    """The register behind the reading is a real one, counted off the served rows."""
    register = _register(route)
    assert ROUTE_COLLECTIONS[route] == (Epoch2Collection.RUN,)
    assert register.count("run") == 2
    assert register.withheld == ()


def test_a_route_that_renders_no_budget_reading_is_refused() -> None:
    """Asking a route for a reading it does not draw would answer for another frame."""
    with pytest.raises(ValueError, match="renders no budget reading"):
        budget_reading(_register("activity"))


def test_an_empty_run_register_still_reads_the_budget_contract() -> None:
    """The contract is stated by the notice, so an empty register does not silence it."""
    reading = budget_reading(_register(COST_CEILING_ROUTE, document={"run": {}}))
    assert reading.control == BUDGET_TERMINATION_CONTROL.value
    assert reading.stopped.state is TruthState.UNKNOWN
