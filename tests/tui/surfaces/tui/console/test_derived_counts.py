"""The spine routes draw daemon-served read models, and every count comes off those rows.

The console used to hold the prototype registers and count them itself, which meant a
count was true of a file rather than of the workspace. The five projection-backed spine
routes now draw a :class:`~eawf.kernel.projection.spine.SpineView` built from the
projection ``projection.<route>.read`` answers, and the frame's counts are read off that
view's rows. The suite pins three things about it.

First, the cursor: a projection's ``source_cursor`` is the document's committed
``canonical_sequence``, decimal and verbatim, so two surfaces holding one cursor hold one
answer. Second, the counts: a register the route binds counts its rows, including counting
zero when it holds nothing, and a count the read model has no register for is absent
rather than zero -- a zero is a count that was taken. Third, the unstated columns: a field
whose producer has not shipped comes back as an unknown truth field naming why, and the
frame prints the truth token for it, so a silent column says it is silent.

The entry layer is here to be excluded. It renders ``process_frame``, the one read model
no projection carries, so it has no read verb at all rather than a read that answers with
nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.read_models import READ_MODEL_BY_KIND, ReadModelKind
from eawf.kernel.projection.spine import (
    ENTRY_ROUTE,
    NATIVE_ROUTES,
    ROUTE_FIELDS,
    SPINE_ROUTES,
    STATUS_FIELD,
    UNPRODUCED_REASON,
    SpineView,
    build_spine_view,
)
from eawf.kernel.projection.truth import TruthKind, TruthState
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.spine import UNAVAILABLE
from eawf.surfaces.tui.console.session import Session

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: The six routes the spine binds: the five a projection carries, and the entry layer.
BOUND_ROUTES: tuple[str, ...] = (ENTRY_ROUTE, *SPINE_ROUTES)

#: One row per collection the spine touches, so a route's own register is never empty by
#: accident and a route that binds two registers can be told apart from one that binds one.
DOCUMENT: dict[str, Any] = {
    "track": {
        "TRK-0001": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0001", "revision": 1, "status": "ACTIVE"},
        "TRK-0002": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0002", "revision": 3, "status": "PAUSED"},
    },
    "milestone": {
        "MLS-0030": {
            "urn": f"urn:eawf:{SCOPE}:milestone:MLS-0030",
            "revision": 2,
            "status": "PLANNED",
        },
    },
    "batch": {
        "BAT-0001": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0001", "revision": 1, "status": "OPEN"},
    },
    "task": {
        "EAWF-0001": {"urn": f"urn:eawf:{SCOPE}:task:EAWF-0001", "revision": 4, "status": "READY"},
    },
    "run": {
        "RUN-9e3779b1": {
            "urn": f"urn:eawf:{SCOPE}:run:RUN-9e3779b1",
            "revision": 7,
            "status": "RUNNING",
        },
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


def _view(route: str, **kwargs: Any) -> SpineView:
    """Return the spine read model of ``route`` over the probe document."""
    return build_spine_view(_projection(route, **kwargs))


def _frame(spine: SpineView, *, width: int = 120) -> tuple[list[str], Session]:
    """Return the console frame ``spine`` renders, and the session it published into."""
    session = Session()
    session.route = spine.route
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=width,
        h=24,
        projection=spine,
    )
    return render_route(view), session


# ---------- the six routes and the verbs that serve them ----------


def test_spine_routes_are_the_five_a_projection_carries() -> None:
    """The spine group names five routes, and the field table names every native one."""
    assert SPINE_ROUTES == ("scope.home", "track", "batch.detail", "task.detail", "run.detail")
    assert set(ROUTE_FIELDS) == set(NATIVE_ROUTES)
    assert set(SPINE_ROUTES) <= set(NATIVE_ROUTES)
    assert ENTRY_ROUTE not in NATIVE_ROUTES


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_every_spine_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


def test_entry_layer_has_no_read_verb_at_all() -> None:
    """The pre-session layer carries no projection, so it is not served rather than empty."""
    kind = REGISTRY.read_models[ENTRY_ROUTE]
    assert kind is ReadModelKind.PROCESS_FRAME
    assert not READ_MODEL_BY_KIND[kind].projection_backed
    assert READ_METHOD_TEMPLATE.format(route=ENTRY_ROUTE) not in ROUTE_READ_METHODS
    assert ENTRY_ROUTE not in ROUTE_COLLECTIONS


@pytest.mark.parametrize("route", BOUND_ROUTES)
def test_every_bound_route_resolves_to_its_declared_read_model(route: str) -> None:
    """The registry row and the kernel declaration name one read model for the route."""
    key = REGISTRY.by_id[route].key
    assert key in READ_MODEL_BY_KIND[REGISTRY.read_models[route]].routes


# ---------- the cursor a read model stands at ----------


@pytest.mark.parametrize("cursor", [0, 1, 2, 41208])
@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_source_cursor_is_the_committed_canonical_sequence(route: str, cursor: int) -> None:
    """The view's cursor is the document's ordinal, decimal and verbatim."""
    spine = _view(route, cursor=cursor)
    assert spine.source_cursor == str(cursor)
    assert spine.read_model is ROUTE_READ_MODELS[route]
    assert spine.scope_id == SCOPE


def test_the_document_states_the_cursor_the_daemon_reads_it_at() -> None:
    """The daemon reads the cursor off the document key the transaction writes."""
    document = {**DOCUMENT, CANONICAL_SEQUENCE_KEY: 41208}
    spine = build_spine_view(
        build_route_projection(
            route="scope.home",
            document=document,
            cursor=document[CANONICAL_SEQUENCE_KEY],
            scope_id=SCOPE,
            generated_at=AT,
        )
    )
    assert spine.source_cursor == "41208"


def test_a_negative_cursor_is_refused() -> None:
    """A cursor is a committed ordinal, so there is no projection before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        _projection("scope.home", cursor=-1)


# ---------- counts derived from the read model ----------


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_every_count_is_the_rows_of_a_register_the_route_binds(route: str) -> None:
    """A count is taken off the view's own rows, one per bound collection."""
    spine = _view(route)
    assert set(spine.counts) == {c.value for c in ROUTE_COLLECTIONS[route]}
    for name, count in spine.counts.items():
        assert count == sum(1 for row in spine.rows if row.collection.value == name)
    assert sum(spine.counts.values()) == len(spine.rows)


def test_scope_home_counts_both_registers_it_binds() -> None:
    """Two registers means two counts; neither borrows the other's rows."""
    spine = _view("scope.home")
    assert spine.count("track") == 2
    assert spine.count("milestone") == 1


def test_a_count_with_no_register_is_absent_rather_than_zero() -> None:
    """A zero is a count that was taken; a count nothing holds is unavailable."""
    spine = _view("scope.home")
    assert spine.count("run") is None
    assert spine.count("") is None


def test_an_empty_register_counts_zero() -> None:
    """A register the route binds and that holds nothing counts zero, honestly."""
    spine = _view("scope.home", document={"track": {}})
    assert spine.counts == {"track": 0, "milestone": 0}
    assert spine.rows == ()


def test_a_single_row_register_counts_one() -> None:
    """The off-by-one boundary below the probe document's two-row register."""
    one = {"track": {"TRK-0001": DOCUMENT["track"]["TRK-0001"]}}
    spine = _view("scope.home", document=one)
    assert spine.count("track") == 1
    assert len(spine.rows) == 1


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_the_frame_prints_the_derived_counts(route: str) -> None:
    """Every count on the frame is one the view derived, and the cursor is beside them."""
    spine = _view(route)
    rows, _session = _frame(spine)
    counts = rows[1]
    for name, count in spine.counts.items():
        assert re.search(rf"\b{count} {name}s?\b", counts), counts
    assert "cursor 41,208" in counts


def test_the_frame_states_the_unavailable_token_when_no_register_is_bound() -> None:
    """A route whose read model binds nothing prints the token, never a bare zero."""
    spine = _view("run.detail", document={})
    empty = SpineView(
        route=spine.route,
        read_model=spine.read_model,
        scope_id=spine.scope_id,
        source_cursor=spine.source_cursor,
        digest=spine.digest,
        complete=spine.complete,
        rows=(),
        counts={},
    )
    rows, _session = _frame(empty)
    assert UNAVAILABLE in rows[1]


def test_an_incomplete_projection_labels_its_counts() -> None:
    """Outside a complete projection a count is a number under a label, not a claim."""
    spine = _view("scope.home")
    partial = SpineView(
        route=spine.route,
        read_model=spine.read_model,
        scope_id=spine.scope_id,
        source_cursor=spine.source_cursor,
        digest=spine.digest,
        complete=False,
        rows=spine.rows,
        counts=spine.counts,
    )
    rows, _session = _frame(partial)
    assert "known" in rows[1]


# ---------- the rows, and the columns nothing states ----------


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_rows_carry_the_status_the_document_states(route: str) -> None:
    """The one produced field is the stored status, and it is stored, not derived."""
    spine = _view(route)
    for row in spine.rows:
        status = row.field(STATUS_FIELD)
        assert status.state is TruthState.KNOWN
        assert status.truth_kind is TruthKind.STORED
        assert status.value == DOCUMENT[row.collection.value][row.key]["status"]
        assert status.provenance_refs == (row.urn,)


def test_a_row_stating_no_status_is_unknown_rather_than_blank() -> None:
    """A record with no status renders the unknown state, which says so."""
    document = {"run": {"RUN-0000": {"urn": f"urn:eawf:{SCOPE}:run:RUN-0000", "revision": 1}}}
    spine = _view("run.detail", document=document)
    status = spine.rows[0].field(STATUS_FIELD)
    assert status.state is TruthState.UNKNOWN
    assert status.value is None
    assert status.missing_reason


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_unproduced_columns_are_unknown_truth_fields_naming_why(route: str) -> None:
    """A dev4 column is declared and comes back unknown, never silently absent."""
    spine = _view(route)
    unproduced = spine.unproduced()
    assert unproduced
    assert STATUS_FIELD not in unproduced
    for row in spine.rows:
        for name in unproduced:
            field = row.field(name)
            assert field.state is TruthState.UNKNOWN
            assert field.value is None
            assert field.missing_reason == UNPRODUCED_REASON
            assert field.truth_kind is TruthKind.DERIVED


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_the_frame_names_every_unstated_column(route: str) -> None:
    """The frame says which columns are silent instead of leaving empty cells."""
    spine = _view(route)
    rows, _session = _frame(spine)
    unstated = next(row for row in rows if row.startswith(" UNSTATED"))
    for name in spine.unproduced():
        assert f"{name} ?" in unstated


@pytest.mark.parametrize("route", SPINE_ROUTES)
def test_the_frame_draws_one_line_per_read_model_row(route: str) -> None:
    """Every row the read model holds reaches the frame, by key and by collection."""
    spine = _view(route)
    rows, _session = _frame(spine)
    body = "\n".join(rows)
    for row in spine.rows:
        assert row.key in body
        assert row.collection.value in body


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    row = _view("track").rows[0]
    with pytest.raises(KeyError):
        row.field("provider")


def test_field_names_lead_with_the_stored_status() -> None:
    """Column order is declared once; the produced field is always first."""
    for route in SPINE_ROUTES:
        assert _view(route).field_names()[0] == STATUS_FIELD


# ---------- refusals ----------


@pytest.mark.parametrize("route", ["activity"])
def test_a_bound_route_with_no_native_read_model_is_refused(route: str) -> None:
    """A projection of another route is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no native read model"):
        build_spine_view(_projection(route))


def test_an_unbound_route_has_no_projection_to_build_from() -> None:
    """A route with no document binding is refused at the projection, not papered over."""
    assert "settings" not in ROUTE_COLLECTIONS
    with pytest.raises(ValueError, match="renders no epoch-2 collection"):
        _projection("settings")
