"""The planning and diagnostics routes draw daemon-served rows, and say what is unstated.

These eight routes used to draw registers the console carried itself: a timeline of three
hand-written lanes, a backlog of two fixed groups, a search page whose hits were a tuple in
the module. A frame like that is true of a file rather than of the workspace, and there is
no cursor it stands at, so two surfaces could not be compared.

They now draw a :class:`~eawf.kernel.projection.spine.SpineView` built from the projection
``projection.<route>.read`` answers, which is the same view the five spine routes draw. The
suite pins three things about that.

First, the binding: every planning and diagnostics route names the collections it renders,
is served by both projection verbs, and draws the rows the daemon projected -- including a
register whose producer is a dev4 item, which counts zero honestly rather than being left
out. Second, the unstated columns: a Campaign, Decision or plan-lens field has no epoch-2
producer, so it comes back as an unknown truth field naming why and the frame prints the
truth token for it. Third, the epoch-1 surfaces are untouched: the research_board mode
still resolves every key its footer advertises.

CON-123 follows: the settings stack renders its field tuple in two tiers, the second tier
only where it is stated.

The file closes on the per-family entity state galleries. CON-103: each family's gallery is
a keyed table of every state its status machine has, counted off the kernel. CON-104: the
states that change layout -- the endings and the two unknowns -- each have a full detail
frame, drawn here and live over a walked tree. CON-105: every state renders through one of
four fixed classes with one clock treatment. CON-106 and CON-107: a merging Batch and a lost
Run are two panes, facts and refused questions, with explicit recovery and no retry.
CON-128: the control-ledger gallery draws all nine outcomes as two-line cells.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from eawf.kernel.projection.compute import (
    DIAGNOSTICS_CORPUS,
    ROUTE_COLLECTIONS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE, RECONNECT_METHOD_TEMPLATE
from eawf.kernel.projection.spine import (
    DIAGNOSTICS_ROUTES,
    NATIVE_ROUTES,
    PLANNING_ROUTES,
    STATUS_FIELD,
    UNPRODUCED_REASON,
    SpineView,
    build_spine_view,
)
from eawf.kernel.projection.truth import TruthKind, TruthState
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.pending_action import PENDING_ACTION_EDGES, PendingActionStatus
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.track import TrackStatus
from eawf.kernel.state.epoch2.transitions import AMBIGUOUS_STATES, TERMINAL_STATUSES, statuses_of
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS, ROUTE_RECONNECT_METHODS
from eawf.surfaces.tui.console import lifecycle
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.cells import NO_VALUE as N
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.decisions import DecisionRecords
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.lifecycle import Elapsed, Family, Layout
from eawf.surfaces.tui.console.mutation import Card, Item, Result
from eawf.surfaces.tui.console.overlays.mutation_card import ledger_cell, results_frame
from eawf.surfaces.tui.console.paint import Part, paint
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from tests.tui.surfaces.tui.console import test_settings_provenance as provenance
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    live_console,
    render_setup,
    walk_canary_isolated,
)

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: The eight routes this suite is about: the planning group and the diagnostics group.
GALLERY_ROUTES: tuple[str, ...] = (*PLANNING_ROUTES, *DIAGNOSTICS_ROUTES)

#: The gallery routes still drawn as the shared record table. Campaign, History diff and
#: Search draw their packet layouts, each silent column a cell wearing the unknown token,
#: and ``test_native_route_frames`` holds those three; the Backlog draws its two groups,
#: which ``test_native_route_bodies`` holds.
TABLE_ROUTES: tuple[str, ...] = tuple(
    r for r in GALLERY_ROUTES if r not in ("campaign", "history.diff", "search", "backlog")
)

#: One row per collection the gallery touches, so no route's register is empty by accident
#: and a route reading across the corpus can be told apart from one reading a single
#: register. The campaign and artifact rows stand in for a producer that ships at dev4.
DOCUMENT: dict[str, Any] = {
    "track": {
        "TRK-0001": {"urn": f"urn:eawf:{SCOPE}:track:TRK-0001", "revision": 1, "status": "ACTIVE"},
    },
    "campaign": {
        "CAM-0001": {
            "urn": f"urn:eawf:{SCOPE}:campaign:CAM-0001",
            "revision": 2,
            "status": "RUNNING",
        },
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
        "BAT-0002": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0002", "revision": 4, "status": "MERGED"},
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
    "artifact": {
        "ART-0001": {"urn": f"urn:eawf:{SCOPE}:artifact:ART-0001", "revision": 1, "status": "KEPT"},
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
    """Return the native read model of ``route`` over the probe document."""
    return build_spine_view(_projection(route, **kwargs))


def _frame(spine: SpineView, *, width: int = 120) -> list[str]:
    """Return the console frame ``spine`` renders on its own route."""
    session = Session()
    session.route = REGISTRY.by_key[spine.route].id
    view = View(
        session=session,
        fixture=load_fixture(
            Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
        ),
        w=width,
        h=24,
        projection=spine,
    )
    return render_route(view)


# ---------- the routes, and the verbs that serve them ----------


def test_the_gallery_routes_are_the_planning_and_diagnostics_groups() -> None:
    """The two groups name the eight routes this wave bound, and nothing else."""
    assert PLANNING_ROUTES == (
        "roadmap",
        "backlog",
        "campaign",
        "campaign.step",
        "campaign.artifact",
    )
    assert DIAGNOSTICS_ROUTES == ("history", "history.diff", "search")
    assert set(GALLERY_ROUTES) <= set(NATIVE_ROUTES)


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_gallery_route_has_both_projection_verbs(route: str) -> None:
    """A route a console draws natively is one the daemon both reads and reconnects."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert RECONNECT_METHOD_TEMPLATE.format(route=route) in ROUTE_RECONNECT_METHODS
    assert ROUTE_COLLECTIONS[route]


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_gallery_route_resolves_to_its_declared_read_model(route: str) -> None:
    """The registry row and the kernel declaration name one read model for the route."""
    spec = REGISTRY.by_key[route]
    assert REGISTRY.read_models[spec.id] is not None


def test_the_renamed_route_reads_under_its_port_key() -> None:
    """The pack calls it ``timeline`` and the port calls it ``roadmap``; the seam uses the key."""
    app = ConsoleApp(
        load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")
    )
    app.session.route = "timeline"

    assert app.route_key == "roadmap"
    assert "roadmap" in ROUTE_COLLECTIONS
    assert app.route_view() is None


# ---------- the rows the daemon served ----------


@pytest.mark.parametrize("route", (*TABLE_ROUTES, "search"))
def test_every_gallery_route_draws_the_rows_the_daemon_projected(route: str) -> None:
    """Every row the read model holds reaches the frame, by key and by collection."""
    spine = _view(route)
    body = "\n".join(_frame(spine))

    assert spine.rows
    for row in spine.rows:
        assert row.key in body
        assert row.collection.value in body
    assert "cursor 41,208" in body


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_count_is_the_rows_of_a_register_the_route_binds(route: str) -> None:
    """A count is taken off the view's own rows, one per bound collection."""
    spine = _view(route)

    assert set(spine.counts) == {c.value for c in ROUTE_COLLECTIONS[route]}
    assert sum(spine.counts.values()) == len(spine.rows)


@pytest.mark.parametrize("route", DIAGNOSTICS_ROUTES)
def test_a_diagnostics_route_reads_across_the_whole_corpus(route: str) -> None:
    """History and search are about records other routes render, so they read that corpus."""
    assert ROUTE_COLLECTIONS[route] == DIAGNOSTICS_CORPUS
    assert set(_view(route).counts) == {c.value for c in DIAGNOSTICS_CORPUS}


def test_a_register_whose_producer_ships_at_dev4_counts_zero_rather_than_vanishing() -> None:
    """The empty boundary: a bound register that holds nothing was still read."""
    spine = _view("campaign", document={"track": DOCUMENT["track"]})

    assert spine.counts == {"campaign": 0}
    assert spine.rows == ()


def test_a_single_row_register_counts_one() -> None:
    """The off-by-one boundary below the probe document's two-row register."""
    one = {"batch": {"BAT-0001": DOCUMENT["batch"]["BAT-0001"]}}
    spine = _view("roadmap", document=one)

    assert spine.count("batch") == 1
    assert spine.count("milestone") == 0
    assert len(spine.rows) == 1


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_rows_carry_the_status_the_document_states(route: str) -> None:
    """The one produced field is the stored status, and it is stored, not derived."""
    for row in _view(route).rows:
        status = row.field(STATUS_FIELD)
        assert status.state is TruthState.KNOWN
        assert status.truth_kind is TruthKind.STORED
        assert status.value == DOCUMENT[row.collection.value][row.key]["status"]


# ---------- the columns whose producers are dev4 items ----------


@pytest.mark.parametrize("route", GALLERY_ROUTES)
def test_every_dev4_column_is_an_unknown_truth_field_naming_why(route: str) -> None:
    """A Campaign, Decision or plan-lens column is declared and comes back unknown."""
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


@pytest.mark.parametrize("route", TABLE_ROUTES)
def test_the_frame_prints_the_unknown_token_beside_every_dev4_column(route: str) -> None:
    """The frame says which columns are silent instead of leaving empty cells."""
    spine = _view(route)
    unstated = next(row for row in _frame(spine) if row.startswith(" UNSTATED"))

    for name in spine.unproduced():
        assert f"{name} ?" in unstated


def test_a_row_names_no_field_the_route_did_not_declare() -> None:
    """Asking a row for an undeclared column raises rather than answering a blank."""
    row = _view("search").rows[0]

    with pytest.raises(KeyError):
        row.field("provider")


# ---------- refusals ----------


def test_a_bound_route_with_no_native_read_model_is_refused() -> None:
    """A projection of another route is not this frame's rows, so it is not adopted."""
    with pytest.raises(ValueError, match="has no native read model"):
        build_spine_view(_projection("activity"))


def test_an_unbound_route_has_no_projection_to_build_from() -> None:
    """A route with no document binding is refused at the projection, not papered over."""
    with pytest.raises(ValueError, match="renders no epoch-2 collection"):
        _projection("settings.stack")


def test_a_negative_cursor_is_refused() -> None:
    """A cursor is a committed ordinal, so there is no projection before the first one."""
    with pytest.raises(ValueError, match="never -1"):
        _projection("campaign", cursor=-1)


# ---------- the epoch-1 surfaces this wave did not touch ----------


def test_the_research_board_mode_resolves_every_key_its_footer_advertises() -> None:
    """The epoch-1 mode keeps working: each advertised token is a key something binds."""
    from eawf.surfaces.tui.app import EaApp
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen

    bound: set[str] = set()
    for klass in (*ResearchBoardModeScreen.__mro__, EaApp):
        for binding in klass.__dict__.get("BINDINGS", ()):
            key = binding.key if hasattr(binding, "key") else binding[0]
            bound.update(key.split(","))
    spelled = {"↑": "up", "↓": "down", "Enter": "enter", "/": "slash", "?": "question_mark"}

    advertised = [hint.split(" ", 1)[0] for hint in ResearchBoardModeScreen.FOOTER_HINTS]
    tokens = [part for token in advertised for part in token.split("/")]

    assert tokens
    for token in tokens:
        # an arrow run advertises two keys in one token; every other token is one key
        glyphs = list(token) if all(glyph in spelled for glyph in token) else [token]
        for glyph in glyphs:
            assert spelled.get(glyph, glyph.lower()) in bound, f"{token} resolves to nothing"


# ---------- CON-123: the settings stack renders the whole field tuple in two tiers ----------


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose config layers the probe owns, its home redirected here."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


def _stack(tree: Path, **leaf: Any) -> list[str]:
    """Return the stack card of ``prose.level`` with ``leaf``'s tuple fields stated."""
    provenance._write(tree / ".ea" / "config.yaml", "prose:\n  level: strict\n")
    view = provenance._view(tree)
    changed = view.leaf(provenance.CATALOG_KEY).model_copy(update=leaf)
    view = view.model_copy(
        update={
            "leaves": tuple(
                changed if item.key == provenance.CATALOG_KEY else item for item in view.leaves
            )
        }
    )
    return provenance._frame("settings.stack", view, key=provenance.CATALOG_KEY, width=80)


def test_con_123_the_first_tier_is_the_nine_layers_winning_lens_and_on_this(tree: Path) -> None:
    frame = _stack(tree)
    head = next(
        i for i, row in enumerate(frame) if re.match(r"^\s+LAYER\s+VALUE\s+KIND\s+WHERE", row)
    )
    layers = [row for row in frame[head + 1 : head + 10] if row.strip()]
    assert len(layers) == 9
    for label in (" WINNING", " LENS", " ON THIS"):
        assert any(row.startswith(label) for row in frame), label
    assert "effective revision" in frame[1]


def test_con_123_an_empty_second_tier_draws_no_row(tree: Path) -> None:
    frame = _stack(tree)
    for label in (" DENIED BY", " CONSTRAINED BY", " NEEDS", " SECRET"):
        assert not any(row.startswith(label) for row in frame), label


def test_con_123_an_authority_denied_key_keeps_the_denied_token_in_its_value(tree: Path) -> None:
    frame = _stack(tree, deny_chain=("org.policy",))
    assert any(row.startswith(" DENIED BY org.policy") for row in frame)
    repo = next(row for row in frame if re.match(r"^\s+.?\s*repo\s", row))
    assert "⊘ strict" in repo


def test_con_123_a_capability_degraded_key_names_its_requirement_and_state(tree: Path) -> None:
    frame = _stack(tree, capability_requirement="cap.prose", certification_state=None)
    needs = next(row for row in frame if row.startswith(" NEEDS"))
    assert "cap.prose" in needs and "certification unknown" in needs


def test_con_123_a_secret_is_named_by_its_reference_never_its_value(tree: Path) -> None:
    frame = _stack(tree, secret_ref="secret://vault/prose")
    secret = next(row for row in frame if row.startswith(" SECRET"))
    assert "secret://vault/prose" in secret and "never renders" in secret


def test_con_123_the_settings_stack_gallery_tells_its_three_key_states_apart(tree: Path) -> None:
    """A plain key, an authority-denied key and a capability-degraded key never read alike."""
    gallery = {
        "plain": _stack(tree),
        "authority-denied": _stack(tree, deny_chain=("org.policy",)),
        "capability-degraded": _stack(
            tree, capability_requirement="cap.prose", certification_state=None
        ),
    }
    bodies = {name: "\n".join(frame[3:-1]) for name, frame in gallery.items()}
    assert len(set(bodies.values())) == 3
    assert "⊘" in bodies["authority-denied"] and "⊘" not in bodies["capability-degraded"]
    assert "certification unknown" in bodies["capability-degraded"]
    assert all(len(frame) == 24 for frame in gallery.values())


# ---------- CON-103 to CON-107: the per-family entity state galleries ----------

ROOT = "eawf://EAWF/EAWF/EAWF"

#: The four class words a gallery legend names, in the packet's own words.
LEGEND = {
    "ok": "a settled good outcome",
    "warn": "needs a person",
    "err": "a bad outcome or an unknown one",
    "info": "waiting, nothing wrong",
}

#: The detail route each family with a native detail frame is drawn on.
DETAIL_ROUTES: dict[Family, str] = {
    Family.TRACK: "track",
    Family.BATCH: "batch.detail",
    Family.TASK: "task.detail",
    Family.RUN: "run.detail",
}


def _gallery(family: Family, *, width: int = 120) -> list[str]:
    """Return one family's gallery: a keyed table of every state, then the class legend."""
    rows = [
        f" Eä ▸ {family.value} states",
        " every state renders distinctly · none may read as another",
        "═" * width,
        f" {'STATE':<32}{'MEANING':<48}RENDERS AS",
        *(
            f" {row.state:<32}{row.meaning:<48}{row.renders_as}"
            for row in lifecycle.TREATMENTS[family]
        ),
        "─" * width,
        " LEGEND    " + " · ".join(f"{k} = {v}" for k, v in LEGEND.items()),
    ]
    return rows


def _record(family: Family, key: str, status: str, **extra: Any) -> dict[str, Any]:
    """Return one stored row of ``family`` in ``status``, filed and stamped like a real one."""
    placements: dict[Family, dict[str, Any]] = {
        Family.BATCH: {
            "milestone_ref": f"{ROOT}/milestone/MLS-0101",
            "target_branch": "main",
            "current_head_binding": {"head_sha": "a1f4c9e" + "0" * 33},
            "task_refs": [f"{ROOT}/task/EAWF-0101"],
        },
        Family.TASK: {
            "intent": "Render the galleries",
            "batch_ref": f"{ROOT}/batch/BAT-0101",
            "priority": "P1",
        },
        Family.RUN: {
            "created_at": "2026-09-17T09:00:00Z",
            "started_at": "2026-09-17T09:00:05Z",
            "scope": {"purpose": "implement", "task_ref": f"{ROOT}/task/EAWF-0101"},
        },
        Family.TRACK: {"title": "Core framework"},
    }
    filed = placements[family]
    return {
        "urn": f"{ROOT}/{family.value}/{key}",
        "revision": 4,
        "status": status,
        "updated_at": "2026-09-17T11:58:02Z",
        **filed,
        **extra,
    }


def _stored(family: Family, state: str) -> str:
    """Return the stored status a gallery state is drawn from: a label rides on its status."""
    if family is Family.RUN and state == "LOST":
        return "RUNNING"
    return state


def _detail(
    family: Family, state: str, *, width: int = 80, lost: bool = False, conn: str = "LIVE"
) -> list[str]:
    """Return the full detail frame of one record of ``family`` in ``state``."""
    key = {Family.TRACK: "TRK-0101", Family.BATCH: "BAT-0101", Family.TASK: "EAWF-0101"}.get(
        family, "RUN-00000101"
    )
    route = DETAIL_ROUTES[family]
    document = {family.value: {key: _record(family, key, _stored(family, state))}}
    session = Session()
    session.route = route
    session.conn = conn
    session.subj_id = key
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=width,
        h=dict(SIZES)[width],
        linked=True,
        principal="OP-0001",
        projection=build_spine_view(_projection(route, document=document)),
        decisions=DecisionRecords(run_states={key: "LOST"}) if lost else None,
    )
    frame = render_route(view)
    assert len(frame) == view.h
    return frame


@pytest.mark.parametrize("family", list(Family))
def test_con_103_every_family_gallery_is_the_census_of_its_status_machine(family: Family) -> None:
    """The gallery lists the kernel's states, in the kernel's order, and adds or drops none."""
    entity = lifecycle.FAMILY_ENTITY[family]
    stored = (
        [s.name for s in PENDING_ACTION_EDGES]
        if entity is None
        else [s.name for s in statuses_of(entity)]
    )
    shown = [row.state for row in lifecycle.TREATMENTS[family]]
    assert shown == list(lifecycle.census(family))
    assert shown[: len(stored)] == stored
    labels = [label.name for (e, _v), label in AMBIGUOUS_STATES.items() if e is entity]
    assert set(shown) == set(stored) | set(labels)


def test_con_103_the_gallery_counts_come_from_the_matrices_not_the_pack() -> None:
    """The pack's own tallies do not sum, so the counts are read, not copied."""
    counts = {family.value: len(lifecycle.TREATMENTS[family]) for family in Family}
    assert counts == {
        "track": len(TrackStatus),
        "milestone": len(MilestoneStatus),
        "batch": len(BatchStatus),
        "task": len(TaskStatus),
        # the lost label renders as a state of its own beside the six stored statuses
        "run": len(RunStatus) + 1,
        "release": len(ReleaseStatus),
        "pending_action": len(PendingActionStatus),
    }


@pytest.mark.parametrize("family", list(Family))
def test_con_103_a_gallery_is_a_keyed_table_in_which_no_two_states_read_alike(
    family: Family,
) -> None:
    rows = _gallery(family)
    body = rows[4 : 4 + len(lifecycle.TREATMENTS[family])]
    keys = [line.split()[0] for line in body]
    assert keys == [row.state for row in lifecycle.TREATMENTS[family]]
    assert len(set(body)) == len(body)
    meanings = [row.meaning for row in lifecycle.TREATMENTS[family]]
    assert len(set(meanings)) == len(meanings), "two states share a meaning"
    for line in body:
        assert " chip · " in line


def test_con_103_a_state_the_kernel_does_not_have_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authored = dict(lifecycle._AUTHORED)
    authored[Family.TRACK] = {
        **authored[Family.TRACK],
        "PAUSED": ("a state no machine has", lifecycle.SeverityClass.WAITING, Elapsed.NONE, ""),
    }
    monkeypatch.setattr(lifecycle, "_AUTHORED", authored)
    with pytest.raises(ValueError, match="track PAUSED is not a state the kernel has"):
        lifecycle._build()


def test_con_103_a_state_the_kernel_has_and_the_gallery_drops_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authored = dict(lifecycle._AUTHORED)
    authored[Family.BATCH] = {k: v for k, v in authored[Family.BATCH].items() if k != "MERGING"}
    monkeypatch.setattr(lifecycle, "_AUTHORED", authored)
    with pytest.raises(ValueError, match="batch MERGING has no treatment"):
        lifecycle._build()


def test_con_103_a_treatment_is_looked_up_by_its_stored_value_or_refused() -> None:
    assert lifecycle.treatment(Family.RELEASE, "publish_timeout").state == "PUBLISH_TIMEOUT"
    with pytest.raises(KeyError, match="run has no state 'MERGING'"):
        lifecycle.treatment(Family.RUN, "MERGING")
    with pytest.raises(KeyError):
        lifecycle.treatment(Family.TRACK, "")


def _layout_states() -> list[tuple[Family, str]]:
    return [
        (family, row.state)
        for family in DETAIL_ROUTES
        for row in lifecycle.TREATMENTS[family]
        if row.layout is not Layout.CHIP
    ]


def test_con_104_the_layout_changing_states_are_the_endings_and_the_unknowns() -> None:
    """An ending changes the frame, an unknown replaces it; a chip-only state does neither."""
    for family in Family:
        for row in lifecycle.TREATMENTS[family]:
            entity = lifecycle.FAMILY_ENTITY[family]
            if row.layout is Layout.UNKNOWN:
                assert (entity, row.state) in {
                    (e, label.name) for (e, _v), label in AMBIGUOUS_STATES.items()
                }
            if entity is not None and row.layout is Layout.FINAL:
                assert row.state.lower() in {s.lower() for s in TERMINAL_STATUSES[entity]}
    assert ("batch", "MERGING") in {(f.value, s) for f, s in _layout_states()}
    assert ("run", "LOST") in {(f.value, s) for f, s in _layout_states()}


@pytest.mark.parametrize(("family", "state"), _layout_states())
@pytest.mark.parametrize("width", [80, 120, 160])
def test_con_104_every_layout_changing_state_has_a_full_detail_frame(
    family: Family, state: str, width: int
) -> None:
    row = lifecycle.treatment(family, state)
    frame = _detail(family, state, width=width, lost=state == "LOST")
    text = "\n".join(frame)
    if row.layout is Layout.UNKNOWN:
        assert any(line.startswith(" WHAT IS TRUE") for line in frame)
        assert any(line.startswith(" WHAT IS NOT known") for line in frame)
        assert "   ROW " not in text, "an unknown state draws no record table"
    else:
        assert any(line.startswith(" FINAL ") and state in line for line in frame), text
        assert ". actions" not in frame[-1], "an ending offers no lifecycle verb"


@pytest.mark.parametrize("family", [Family.TRACK, Family.BATCH, Family.TASK])
def test_con_104_a_chip_only_state_keeps_the_layout_and_states_its_subject(
    family: Family,
) -> None:
    state = next(r.state for r in lifecycle.TREATMENTS[family] if r.layout is Layout.CHIP)
    frame = _detail(family, state)
    treated = lifecycle.treatment(family, state)
    assert next(line for line in frame if line.startswith(" STATE ")).split()[1] == state
    assert treated.meaning in "\n".join(frame)
    assert any(line.lstrip().startswith("ROW ") for line in frame), "the record table stays"
    assert not any(line.startswith(" FINAL ") for line in frame)


def test_con_104_a_detail_frame_opens_with_the_cursor_on_its_subject() -> None:
    document = {
        "batch": {key: _record(Family.BATCH, key, "ACTIVE") for key in ("BAT-0100", "BAT-0101")}
    }
    session = Session()
    session.route = "batch.detail"
    session.subj_id = "BAT-0101"
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=80,
        h=24,
        linked=True,
        projection=build_spine_view(_projection("batch.detail", document=document)),
    )
    frame = render_route(view)
    assert any(line.startswith(" ▸ BAT-0101") for line in frame)
    assert session.sel_id == "BAT-0101"


def test_con_105_every_state_renders_through_exactly_one_of_four_fixed_classes() -> None:
    assert {c.value: m for c, m in lifecycle.SEVERITY_MEANING.items()} == LEGEND
    for family in Family:
        for row in lifecycle.TREATMENTS[family]:
            assert isinstance(row.severity, lifecycle.SeverityClass)
            assert lifecycle.WORD_CLASSES[row.state] is row.severity


@pytest.mark.parametrize("family", list(Family))
def test_con_105_the_painter_colours_each_state_word_in_its_one_class(
    family: Family,
) -> None:
    for row in lifecycle.TREATMENTS[family]:
        line = f"   {row.state:<32}{row.meaning}"
        strokes = paint(line, Part.BODY)
        word = next(s for s in strokes if s.text == row.state)
        assert word.surface == row.severity.value, row.state


def test_con_105_the_clock_is_fixed_per_state_and_never_implies_a_denied_liveness() -> None:
    for family in Family:
        entity = lifecycle.FAMILY_ENTITY[family]
        for row in lifecycle.TREATMENTS[family]:
            if row.layout is Layout.FINAL:
                assert row.elapsed is Elapsed.FINAL, row
            if row.layout is Layout.UNKNOWN:
                assert row.elapsed is Elapsed.PARTIAL, row
            if row.state in {"QUEUED", "DRAFT", "PLANNED", "CREATED"}:
                assert row.elapsed is Elapsed.NONE, row
            if row.state == "SUSPENDED":
                assert row.elapsed is Elapsed.FROZEN
            if entity is not None and row.state == "RUNNING":
                assert row.elapsed is Elapsed.LIVE


@pytest.mark.parametrize(("family", "state"), [(Family.TASK, "RUNNING"), (Family.BATCH, "ACTIVE")])
def test_con_105_the_detail_frame_states_the_clock_its_state_allows(
    family: Family, state: str
) -> None:
    frame = _detail(family, state)
    clock = next(line for line in frame if line.startswith(" CLOCK "))
    words = lifecycle.ELAPSED_WORDS[lifecycle.treatment(family, state).elapsed]
    assert words in clock and "last moved 11:58:02" in clock


def test_con_105_an_ending_without_its_final_clock_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authored = dict(lifecycle._AUTHORED)
    run = dict(authored[Family.RUN])
    meaning, severity, _clock, note = run["COMPLETED"]
    run["COMPLETED"] = (meaning, severity, Elapsed.LIVE, note)
    authored[Family.RUN] = run
    monkeypatch.setattr(lifecycle, "_AUTHORED", authored)
    with pytest.raises(ValueError, match="run COMPLETED is an ending without its final clock"):
        lifecycle._build()


def test_con_105_an_unknown_that_reads_as_success_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authored = dict(lifecycle._AUTHORED)
    batch = dict(authored[Family.BATCH])
    meaning, _cls, clock, note = batch["MERGING"]
    batch["MERGING"] = (meaning, lifecycle.SeverityClass.SETTLED, clock, note)
    authored[Family.BATCH] = batch
    monkeypatch.setattr(lifecycle, "_AUTHORED", authored)
    with pytest.raises(ValueError, match="batch MERGING would read as a settled good outcome"):
        lifecycle._build()


def _panes(frame: list[str]) -> tuple[list[str], list[str]]:
    """Return the lines of the what-is-true pane and of the what-is-not-known pane."""
    top = next(i for i, line in enumerate(frame) if line.startswith(" WHAT IS TRUE"))
    mid = next(i for i, line in enumerate(frame) if line.startswith(" WHAT IS NOT known"))
    true = [line for line in frame[top + 1 : mid] if line.startswith("   ")]
    rest = frame[mid + 1 :]
    unknown = list(rest[: next(i for i, x in enumerate(rest) if "─" in x)])
    return true, unknown


@pytest.mark.parametrize(("family", "state"), [(Family.BATCH, "MERGING"), (Family.RUN, "LOST")])
def test_con_106_the_two_unknown_states_are_facts_with_timestamps_and_refused_questions(
    family: Family, state: str
) -> None:
    frame = _detail(family, state, lost=state == "LOST")
    true, unknown = _panes(frame)
    assert true and unknown
    for line in true:
        assert re.search(r"\d\d:\d\d:\d\d", line), f"a fact without its instant: {line!r}"
    for line in unknown:
        assert line.strip().startswith("whether "), f"not a question: {line!r}"
    text = [line.strip() for line in frame]
    assert "This state means we do not know. It is not success and not failure," in text
    assert "and it raises a pause rather than resolving itself." in text
    for word in ("COMPLETED", "FAILED", "SUCCEEDED", "success ·"):
        assert word not in frame[1], "the unknown state reads as an outcome"


def test_con_106_a_running_run_nobody_reported_lost_keeps_its_ordinary_frame() -> None:
    frame = _detail(Family.RUN, "RUNNING")
    assert not any(line.startswith(" WHAT IS TRUE") for line in frame)
    assert next(line for line in frame if line.startswith(" STATE")).split()[1] == "RUNNING"


def test_con_106_only_the_two_unknown_families_draw_the_two_panes() -> None:
    from eawf.surfaces.tui.console.renderers.detail import unknown_frame

    spine = build_spine_view(
        _projection(
            "track",
            document={"track": {"TRK-0101": _record(Family.TRACK, "TRK-0101", "ACTIVE")}},
        )
    )
    session = Session()
    session.route = "track"
    view = View(session=session, fixture=Fixture.from_chrome(load_chrome()), w=80, h=24)
    with pytest.raises(ValueError, match="not a record whose outcome the registry calls unknown"):
        unknown_frame(view, spine, spine.rows[0], ())


def test_con_107_a_lost_run_offers_resume_and_let_go_and_never_retry() -> None:
    frame = _detail(Family.RUN, "LOST", lost=True)
    start = next(i for i, line in enumerate(frame) if line.startswith(" RECOVERY"))
    recovery = " ".join(line.strip() for line in frame[start : start + 3])
    assert "resume waits for the same Run" in recovery
    assert "let go closes it as CANCELLED" in recovery
    assert "neither is a guess about what happened" in recovery
    assert "retry is not offered" in recovery
    assert "retry" not in frame[-1].lower()
    assert "cancel" not in recovery.split("let go", 1)[0]


def test_con_107_a_merging_batch_offers_reconcile_only_and_says_why_not_retry() -> None:
    frame = _detail(Family.BATCH, "MERGING")
    start = next(i for i, line in enumerate(frame) if line.startswith(" RECOVERY"))
    recovery = " ".join(line.strip() for line in frame[start : start + 2])
    assert "reconcile asks the host what is true" in recovery
    assert "retry is not offered" in recovery
    assert "because a second merge could duplicate the first" in recovery
    assert "retry" not in frame[-1].lower()


def test_con_107_recovery_lands_only_where_the_registry_allows() -> None:
    assert lifecycle.recovery_landing(Family.RUN) == ("CANCELLED",)
    assert lifecycle.recovery_landing(Family.BATCH) == ("CANCELLED", "FAILED")
    assert lifecycle.recovery_landing(Family.TRACK) == ()


@pytest.mark.parametrize("conn", ["DISCONNECTED", "REPLAYING"])
def test_con_106_an_unknown_frame_read_from_an_old_read_still_says_so(conn: str) -> None:
    frame = _detail(Family.BATCH, "MERGING", conn=conn)
    assert any(line.startswith(" ATTACHED") for line in frame)
    assert any(line.startswith(" WHAT IS TRUE") for line in frame)


# ---------- CON-128: the control-ledger gallery ----------

LEDGER_FIXTURE = Path(__file__).resolve().parents[4] / "fixtures/console/control-ledger.json"


class _LedgerCell(BaseModel):
    """One gallery cell as the fixture states it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: ControlDisposition
    control: str
    target: str
    issued_by: str
    stamps: tuple[str, str, str]
    note: str


class _Ledger(BaseModel):
    """The control-ledger gallery fixture: one cell per outcome, and every outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cells: tuple[_LedgerCell, ...]

    @model_validator(mode="after")
    def _every_outcome_once(self) -> _Ledger:
        found = [cell.disposition for cell in self.cells]
        if sorted(found) != sorted(ControlDisposition):
            raise ValueError(f"the gallery needs all nine outcomes once, got {len(found)}")
        return self


def _ledger(raw: dict[str, Any] | None = None) -> _Ledger:
    data = json.loads(LEDGER_FIXTURE.read_text(encoding="utf-8")) if raw is None else raw
    return _Ledger.model_validate(data)


def _cells() -> dict[ControlDisposition, tuple[str, str]]:
    return {
        cell.disposition: ledger_cell(
            cell.disposition,
            control=cell.control,
            target=cell.target,
            issued_by=cell.issued_by,
            stamps=cell.stamps,
            note=cell.note,
        )
        for cell in _ledger().cells
    }


def test_con_128_the_gallery_renders_all_nine_outcomes_as_two_line_cells() -> None:
    cells = _cells()
    assert len(cells) == 9 == len(ControlDisposition)
    for disposition, (chip, stamps) in cells.items():
        assert chip.startswith(f"{disposition.name}  ")
        assert " issued by " in chip
        assert re.match(r"^req \S+  acc \S+  cfm \S+  \S", stamps), stamps
    assert len({chip for chip, _s in cells.values()}) == 9


def test_con_128_the_two_cells_the_design_drew_nowhere_read_as_the_packet_states() -> None:
    cells = _cells()
    assert cells[ControlDisposition.INVALIDATED] == (
        "INVALIDATED  answer · ACT-0901  issued by operator (you)",
        f"req 13:00:02  acc {N}  cfm {N}  bound revision 6 moved to 7 · reconcile first",
    )
    assert cells[ControlDisposition.SUPERSEDED] == (
        "SUPERSEDED  answer · ACT-0901  issued by operator (you)",
        f"req {N}  acc {N}  cfm {N}  bo answered first at 41,209 · nothing written",
    )


def test_con_128_a_stale_revision_is_invalidated_never_rejected() -> None:
    stale = next(c for c in _ledger().cells if "moved to" in c.note)
    assert stale.disposition is ControlDisposition.INVALIDATED
    assert not _cells()[stale.disposition][0].startswith("REJECTED")


def test_con_128_unknown_keeps_its_accepted_stamp_and_superseded_suppresses_every_stamp() -> None:
    cells = _cells()
    assert cells[ControlDisposition.UNKNOWN][1].startswith(f"req 13:00:02  acc 13:00:03  cfm {N}")
    superseded = next(c for c in _ledger().cells if c.disposition is ControlDisposition.SUPERSEDED)
    assert superseded.stamps[0] == "13:00:02", "the fixture states the keypress time"
    assert cells[ControlDisposition.SUPERSEDED][1].startswith(f"req {N}  acc {N}  cfm {N}")


def test_con_128_a_gallery_with_fewer_than_nine_cells_is_refused() -> None:
    raw = json.loads(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    raw["cells"] = raw["cells"][:8]
    with pytest.raises(ValidationError, match="all nine outcomes"):
        _ledger(raw)
    raw["cells"] = []
    with pytest.raises(ValidationError, match="all nine outcomes"):
        _ledger(raw)


def test_con_128_a_cell_naming_no_known_outcome_is_refused() -> None:
    raw = json.loads(LEDGER_FIXTURE.read_text(encoding="utf-8"))
    raw["cells"][0]["disposition"] = "failed"
    with pytest.raises(ValidationError):
        _ledger(raw)


def test_con_128_the_results_card_draws_the_ledger_cell_of_the_row_under_the_cursor() -> None:
    item = Item(
        key="ACT-0901",
        title=None,
        revision=6,
        status="WAITING",
        effects=("answers it",),
        not_effects=(),
        refusal=None,
        unknown="",
        request=None,
        stale_token="6",
    )
    card = Card(
        kind="answer",
        origin="a",
        action="answer",
        noun="action",
        items=(item,),
        if_stale="reload",
        authority="answer · acting as OP-0001",
        opened_at=0.0,
        issuer="OP-0001",
        results=(
            Result(
                key="ACT-0901",
                requested="+0.2s",
                disposition=ControlDisposition.INVALIDATED,
                detail="bound revision 6 moved to 7",
            ),
        ),
    )
    session = Session()
    view = View(session=session, fixture=Fixture.from_chrome(load_chrome()), w=120, h=40)
    frame = results_frame(view, card)
    at = next(i for i, line in enumerate(frame) if line.startswith(" LEDGER"))
    assert "INVALIDATED  answer · ACT-0901  issued by OP-0001" in frame[at]
    assert f"req +0.2s  acc {N}  cfm {N}" in frame[at + 1]


# ---------- the live path: detail frames of a walked tree's real records ----------


def test_con_104_the_live_detail_frames_draw_the_walked_trees_own_records(tmp_path: Path) -> None:
    """Each detail route, opened on a real record, states that record's own state."""
    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> dict[str, tuple[str, Any]]:
        out: dict[str, tuple[str, Any]] = {}
        async with (
            live_console(walk.canary.root, runtime_root) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            for route in ("track", "batch.detail", "task.detail"):
                await render_setup(app, pilot, SessionSetup(route=route, size=1))
                held = seam.projection_for(route)
                assert held is not None and held.rows, f"the walked tree holds no {route} row"
                subject = held.rows[0]
                text = await render_setup(
                    app, pilot, SessionSetup(route=route, subjId=subject.key, size=1)
                )
                out[route] = (text, subject)
        return out

    frames = asyncio.run(body())
    for route, (text, subject) in frames.items():
        lines = text.split("\n")
        status = subject.status.value
        family = Family(subject.collection.value)
        treated = lifecycle.treatment(family, status)
        if treated.layout is Layout.UNKNOWN:
            assert any(line.startswith(" WHAT IS TRUE") for line in lines), route
            continue
        subject_line = next(line for line in lines if line.startswith(" SUBJECT"))
        assert subject.key in subject_line, route
        state_line = next(line for line in lines if line.startswith(" STATE"))
        assert f"{treated.state} · {treated.meaning}" in state_line, route
