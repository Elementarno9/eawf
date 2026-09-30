"""The Attention projection: its producer, its buckets, its audience and its one gesture.

UI-062. The Attention route's eight exception buckets, with the ``AttentionNeedKind``
needs under ``needs operator``, partition the register totally and disjointly: every open
item lands in exactly one bucket and, under ``needs operator``, exactly one need; the
strip, the rail and the summary line are one derivation; a bucket filter never changes a
rail count; a bucket whose producer this projection does not carry states the unknown
token, and a declared hole states a zero that is never a count of an undeclared source.

UI-017. Counts use the exact audience: an item addressed to one principal is that
principal's ``mine`` and nobody else's, an unaddressed item is everyone's, one principal's
disposition changes no other principal's count, and a resolution is attributable and
compare-and-swap safe.

UI-028. The header's ``!N NEEDS YOU`` is this principal's own open non-notice count at
the rendered revision, the same number as the route's ``mine``; it never renders ``!0``
and never twice on a frame. ``g n`` opens the route and ``!`` jumps to the top item from
any route.

UI-009. Each item keeps its source record and exact revision; an answer is addressed to
that revision, and a refusal for a revision the record has moved past re-reads the record
rather than leaving the stale one on screen.

The pending-action register has a producer: the acceptance-approval verb opens and seals
protected approvals, so the register is written and counted, never withheld.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.attention import (
    BUCKET_SOURCES,
    NO_PRINCIPAL_MINE_REASON,
    AttentionBucket,
    AttentionNeedKind,
    BucketSource,
    attention_all,
    attention_mine,
    build_attention_view,
)
from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    KeyedPatch,
    RouteProjection,
    build_route_projection,
    patches_for_event,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    BUDGET_UNSTATED_REASON,
    COST_CEILING_ROUTE,
    NOTIFICATIONS_ROUTE,
    UNWRITTEN_COLLECTIONS,
    RegisterView,
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
from eawf.kernel.state.epoch2.pending_action import AnswerOutcome, HumanPrincipal, PendingAction
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.console.attention import open_actions
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View, needs_count
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import (
    AnswerRequest,
    OperationResult,
    OperationStatus,
    Operator,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import attention as attention_renderer
from eawf.surfaces.tui.console.renderers import render_route
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
        "ACT-0001": {"urn": ACTION_URN, "revision": 1, "status": "WAITING"},
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


class _Host:
    """The dispatcher's host: a held clock and a quit nothing here asks for."""

    def __init__(self) -> None:
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """End the session; unreachable from the keys these tests press."""


def _frame_fixture() -> Fixture:
    """Return the tracked prototype fixture the epoch-1 mode draws."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _frame(register: RegisterView) -> tuple[list[str], Session]:
    """Return the native frame ``register`` renders, and the session it published into."""
    session = Session()
    session.route = register.route
    view = View(
        session=session,
        fixture=_frame_fixture(),
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


# ---------- the register has a producer, and is counted ----------


#: A register as the acceptance-approval producer writes it: two waiting questions, one
#: addressed to OP-0001 and one to everyone, one not yet asked, and one already sealed.
AUDIENCE: dict[str, Any] = {
    "pending_action": {
        "ACT-0001": {
            "urn": ACTION_URN,
            "revision": 1,
            "status": "WAITING",
            "assignee_ref": "OP-0001",
        },
        "ACT-0002": {"urn": ACTION_URN.replace("0001", "0002"), "revision": 3, "status": "WAITING"},
        "ACT-0003": {"urn": ACTION_URN.replace("0001", "0003"), "revision": 1, "status": "CREATED"},
        "ACT-0004": {"urn": ACTION_URN.replace("0001", "0004"), "revision": 2, "status": "SEALED"},
    }
}


def _attention(document: dict[str, Any] | None = None, **kwargs: Any) -> RegisterView:
    """Return the Attention register over ``document``, the audience probe by default."""
    return _register(ATTENTION_ROUTE, document=AUDIENCE if document is None else document, **kwargs)


def test_the_attention_route_binds_the_pending_action_permission_and_question_registers() -> None:
    """The route reads pending actions, provider permissions and questions; all are written."""
    assert ROUTE_COLLECTIONS[ATTENTION_ROUTE] == (
        Epoch2Collection.PENDING_ACTION,
        Epoch2Collection.PERMISSION,
        Epoch2Collection.OPEN_QUESTION,
    )
    assert Epoch2Collection.PENDING_ACTION not in UNWRITTEN_COLLECTIONS
    assert Epoch2Collection.PERMISSION not in UNWRITTEN_COLLECTIONS
    assert Epoch2Collection.OPEN_QUESTION not in UNWRITTEN_COLLECTIONS


def test_the_written_register_is_counted_rather_than_withheld() -> None:
    """A register a producer writes states its rows, so zero there is a count."""
    register = _attention()
    assert register.withheld == ()
    assert register.count("pending_action") == 4
    assert _attention({}).count("pending_action") == 0


# ---------- UI-062: eight buckets partition the register ----------


def test_ui_062_the_buckets_are_eight_in_severity_order() -> None:
    """The order is fixed and severity-first, and ``needs operator`` carries three needs."""
    assert [b.value for b in AttentionBucket] == [
        "failed",
        "lost",
        "needs operator",
        "stalled",
        "over budget",
        "rejected",
        "active",
        "queued",
    ]
    assert [n.value for n in AttentionNeedKind] == ["permission", "answer", "readiness"]


def test_ui_062_every_open_item_lands_in_exactly_one_bucket_and_one_need() -> None:
    """The partition is total and disjoint; a sealed question is no longer open."""
    view = build_attention_view(_attention())
    assert [i.key for i in view.items] == ["ACT-0001", "ACT-0002", "ACT-0003"]
    placed = {i.key: (i.bucket, i.need) for i in view.items}
    assert placed["ACT-0001"] == (AttentionBucket.NEEDS_OPERATOR, AttentionNeedKind.ANSWER)
    assert placed["ACT-0003"] == (AttentionBucket.QUEUED, None)
    counted = {(c.bucket, c.need): c.count for c in view.bucket_counts()}
    tops = [c for c in view.bucket_counts() if c.need is None and c.count is not None]
    assert sum(c.count or 0 for c in tops) == len(view.items)
    needs = [c.count or 0 for c in view.bucket_counts() if c.need is not None]
    assert sum(needs) == counted[(AttentionBucket.NEEDS_OPERATOR, None)] == 2


def test_ui_062_holes_state_zero_and_unstated_buckets_state_no_count() -> None:
    """A hole's zero is declared; a bucket whose producer is off this register is unknown."""
    counts = {
        c.bucket: c for c in build_attention_view(_attention()).bucket_counts() if c.need is None
    }
    for hole in (AttentionBucket.STALLED, AttentionBucket.REJECTED, AttentionBucket.ACTIVE):
        assert BUCKET_SOURCES[hole] is BucketSource.HOLE
        assert counts[hole].count == 0
        assert counts[hole].reason
    for unstated in (AttentionBucket.FAILED, AttentionBucket.LOST, AttentionBucket.OVER_BUDGET):
        assert counts[unstated].count is None
        assert counts[unstated].reason
    assert counts[AttentionBucket.OVER_BUDGET].reason == BUDGET_UNSTATED_REASON


def test_ui_062_a_row_stating_no_status_lands_in_no_bucket() -> None:
    """An unstated row is filed under no bucket it never named."""
    view = build_attention_view(
        _attention({"pending_action": {"ACT-0009": {"urn": ACTION_URN, "revision": 1}}})
    )
    assert view.items == ()


def test_ui_062_the_strip_the_rail_and_the_summary_are_one_derivation() -> None:
    """The strip's ``all``, the rail's rows and the summary line state the same numbers."""
    register = _attention()
    items = attention_renderer.bucket_items(register)
    rail = attention_renderer.rail_lines(register, None)
    assert items[0].n == 3
    assert [line.split()[-1] for line in rail[1:]] == [str(x.n) for x in items[1:]]
    assert "3 all principals" in attention_renderer.counts_line(register, principal="OP-0002")
    assert any(line.startswith("   ↳ answer") and line.endswith("2") for line in rail)
    assert any(line.startswith(" failed") and line.rstrip().endswith("?") for line in rail)


def test_ui_062_a_bucket_filter_never_changes_a_rail_count() -> None:
    """Filtering what the body lists leaves every bucket's count where it was."""
    register = _attention()
    unfiltered, _ = _frame(register)
    session = Session()
    session.route = ATTENTION_ROUTE
    session.bucket = "queued"
    view = View(
        session=session,
        fixture=_frame_fixture(),
        w=120,
        h=24,
        register=register,
        attention=register,
    )
    filtered = render_route(view)
    # the caret moves to the chosen bucket; no count beside any bucket moves with it
    rail = [row.split("│ ", 1)[1] for row in unfiltered if "│ " in row]
    assert [row.replace("▸", " ") for row in rail] == [
        row.split("│ ", 1)[1].replace("▸", " ") for row in filtered if "│ " in row
    ]


def test_ui_062_asking_another_route_for_attention_items_raises() -> None:
    """Rows that are not actions cannot be bucketed as actions."""
    with pytest.raises(ValueError, match="states no attention items"):
        build_attention_view(_register("activity"))


# ---------- UI-017: counts use the exact audience ----------


def test_ui_017_an_addressed_item_is_its_principals_and_nobody_elses() -> None:
    """OP-0001 counts the item addressed to it and the unaddressed one; OP-0002 only the latter."""
    register = _attention()
    assert attention_mine(register, principal="OP-0001").value == "3"
    assert attention_mine(register, principal="OP-0002").value == "2"
    assert attention_all(register).value == "3"


def test_ui_017_a_console_acting_as_nobody_has_no_mine() -> None:
    """The unknown token stands where the count would be, never everyone's count."""
    mine = attention_mine(_attention(), principal=None)
    assert mine.state is TruthState.UNKNOWN
    assert mine.missing_reason == NO_PRINCIPAL_MINE_REASON


def test_ui_017_one_principals_disposition_changes_no_other_count() -> None:
    """A losing answer is a per-principal row beside the seal; it moves nobody's count."""
    action = PendingAction.model_validate(
        {
            "id": "ACT-0001",
            "urn": ACTION_URN,
            "kind": "operator_decision",
            "subject_ref": ACTION_URN.replace("pending-action/ACT-0001", "milestone/MLS-0001"),
            "question": "Proceed?",
            "options": [
                {"option_id": "yes", "label": "Yes", "effect": "approve"},
                {"option_id": "no", "label": "No", "effect": "decline"},
            ],
            "idempotency_key": "req-0001",
            "status": "WAITING",
            "requested_by": {"principal_kind": "human", "principal_id": "OP-0003"},
            "assignee_ref": "OP-0001",
            "created_at": AT.isoformat(),
            "updated_at": AT.isoformat(),
        }
    )
    noted = action.with_disposition(
        principal_id="OP-0002", outcome=AnswerOutcome.SUPERSEDED, option_id="no"
    )
    document = {"pending_action": {"ACT-0001": noted.model_dump(mode="json")}}
    register = _attention(document)
    assert attention_mine(register, principal="OP-0001").value == "1"
    assert attention_mine(register, principal="OP-0002").value == "0"


def test_ui_017_a_global_resolution_is_attributable_and_compare_and_swap_safe() -> None:
    """The seal names who resolved it, a stale answer is refused, and it leaves every count."""
    action = PendingAction.model_validate(
        {
            "id": "ACT-0001",
            "urn": ACTION_URN,
            "kind": "operator_decision",
            "subject_ref": ACTION_URN.replace("pending-action/ACT-0001", "milestone/MLS-0001"),
            "question": "Proceed?",
            "options": [
                {"option_id": "yes", "label": "Yes", "effect": "approve"},
                {"option_id": "no", "label": "No", "effect": "decline"},
            ],
            "idempotency_key": "req-0001",
            "status": "WAITING",
            "revision": 2,
            "requested_by": {"principal_kind": "human", "principal_id": "OP-0003"},
            "created_at": AT.isoformat(),
            "updated_at": AT.isoformat(),
        }
    )
    resolver = HumanPrincipal(principal_kind="human", principal_id="OP-0001")
    receipt = ACTION_URN.replace("pending-action/ACT-0001", "evidence/EVD-0001")
    with pytest.raises(ValueError, match="not the 1 the answer was given against"):
        action.answer(
            expected_revision=1, resolver=resolver, option_id="yes", receipt_ref=receipt, at=AT
        )
    sealed = action.answer(
        expected_revision=2, resolver=resolver, option_id="yes", receipt_ref=receipt, at=AT
    ).action
    assert sealed.resolution_actor == resolver
    register = _attention({"pending_action": {"ACT-0001": sealed.model_dump(mode="json")}})
    for principal in ("OP-0001", "OP-0002"):
        assert attention_mine(register, principal=principal).value == "0"


# ---------- UI-028: the header count and the one gesture ----------


def _header_view(register: RegisterView, *, route: str, principal: str | None) -> View:
    """Return a linked render view on ``route`` holding the Attention register."""
    session = Session()
    session.route = route
    return View(
        session=session,
        fixture=_frame_fixture(),
        w=120,
        h=24,
        register=register if route == ATTENTION_ROUTE else None,
        attention=register,
        linked=True,
        principal=principal,
    )


def test_ui_028_the_header_count_is_this_principals_mine_at_the_same_revision() -> None:
    """The corner and the route's ``mine`` are one number, per principal."""
    register = _attention()
    for principal, n in (("OP-0001", 3), ("OP-0002", 2)):
        view = _header_view(register, route=ATTENTION_ROUTE, principal=principal)
        assert needs_count(view) == n
        frame = render_route(view)
        assert f"!{n} NEEDS YOU" in frame[0]
        assert frame[1].startswith(f" {n} mine · 3 all principals")


def test_ui_028_the_phrase_renders_once_and_never_as_zero() -> None:
    """``NEEDS YOU`` appears on one row of a frame, and a zero count renders no badge."""
    register = _attention()
    frame = render_route(_header_view(register, route=ATTENTION_ROUTE, principal="OP-0002"))
    assert sum(row.count("NEEDS YOU") for row in frame) == 1
    quiet = render_route(_header_view(_attention({}), route=ATTENTION_ROUTE, principal="OP-0002"))
    assert not any("NEEDS YOU" in row or "!0" in row for row in quiet)
    nobody = render_route(_header_view(register, route=ATTENTION_ROUTE, principal=None))
    assert "NEEDS YOU" not in nobody[0]


def test_ui_028_the_count_travels_to_every_route() -> None:
    """The badge is read off the Attention register whatever route is drawn."""
    view = _header_view(_attention(), route="activity", principal="OP-0001")
    assert "!3 NEEDS YOU" in render_route(view)[0]


def test_ui_028_g_n_opens_the_attention_route() -> None:
    """The route is one ``g`` letter away from every route."""
    assert REGISTRY.go_map["n"] == ATTENTION_ROUTE


def _press(
    key: str, *, route: str, attention: RouteProjection | None, principal: str | None
) -> Session:
    """Press ``key`` on ``route`` and return the session it left."""
    session = Session()
    session.route = route
    ctx = Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()) if attention is not None else _frame_fixture(),
        host=_Host(),
        w=120,
        h=24,
        attention=attention,
        principal=principal,
    )
    dispatch(ctx, key, False)
    return session


def test_ui_028_bang_jumps_to_this_principals_top_item_from_any_route() -> None:
    """``!`` opens the top item's decision surface and keeps the route it left (CON-149)."""
    held = _projection(ATTENTION_ROUTE, document=AUDIENCE)
    for route in ("activity", "scope.home", "roadmap"):
        session = _press("!", route=route, attention=held, principal="OP-0002")
        assert session.route == ATTENTION_ROUTE
        assert session.sel_id == "ACT-0002"
        assert session.overlay == "consequence"
        assert [entry.route for entry in session.back.entries] == [route]
    mine_first = _press("!", route="activity", attention=held, principal="OP-0001")
    assert mine_first.sel_id == "ACT-0001"


def test_ui_028_bang_with_nothing_open_stays_put() -> None:
    """With nothing open the key says so rather than opening an empty route."""
    session = _press(
        "!",
        route="activity",
        attention=_projection(ATTENTION_ROUTE, document={}),
        principal="OP-0001",
    )
    assert session.route == "activity"
    assert session.log[-1].note == "nothing needs you"


def test_ui_028_bang_on_the_prototype_register_selects_its_first_open_action() -> None:
    """A console with no link reads its prototype register, so the golden mode still jumps."""
    session = _press("!", route="activity", attention=None, principal=None)
    fixture = _frame_fixture()
    assert session.route == ATTENTION_ROUTE
    assert session.sel_id == open_actions(fixture)[0].id


# ---------- UI-009: source ledger and exact revision; stale CAS re-reads ----------


def test_ui_009_each_item_keeps_its_source_record_and_exact_revision() -> None:
    """An item is the register row it came from, addressed and revisioned as stored."""
    view = build_attention_view(_attention())
    item = next(i for i in view.items if i.key == "ACT-0002")
    assert item.source_ref == ACTION_URN.replace("0001", "0002")
    assert item.revision == 3


class _StaleDaemon:
    """A daemon whose record moves on between the console's read and its answer."""

    def __init__(self) -> None:
        self.reads = 0
        self.writes: list[dict[str, Any]] = []

    def client(self) -> _StaleDaemon:
        return self

    def __enter__(self) -> _StaleDaemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == READ_METHOD_TEMPLATE.format(route=ATTENTION_ROUTE):
            self.reads += 1
            revision = 1 if self.reads == 1 else 2
            row = {"urn": ACTION_URN, "revision": revision, "status": "WAITING"}
            return _projection(
                ATTENTION_ROUTE,
                cursor=41208 + self.reads,
                document={"pending_action": {"ACT-0001": row}},
            ).model_dump(mode="json")
        self.writes.append(dict(params or {}))
        raise DaemonRpcError(-32602, "validation_failed: revision_conflict: ACT-0001 moved on")


def test_ui_009_an_answer_is_addressed_to_the_revision_shown_and_a_stale_one_rereads() -> None:
    """The refused answer wrote nothing, and the console now holds the record as it stands."""
    daemon = _StaleDaemon()
    seam = ProjectionSeam(
        route=ATTENTION_ROUTE,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        daemon_client_factory=daemon.client,
        operator=Operator(
            principal="OP-0001",
            receipt_ref=ACTION_URN.replace("pending-action/ACT-0001", "evidence/EVD-0001"),
        ),
    )

    async def drive() -> OperationResult:
        await seam.load()
        return await seam.request(AnswerRequest(target="ACT-0001", option_id="approve"))

    result = asyncio.run(drive())
    assert result.status is OperationStatus.REFUSED
    assert daemon.writes[0]["expected_revision"] == 1
    assert daemon.reads == 2
    assert seam.projection is not None
    assert seam.projection.rows[0].revision == 2


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


def test_the_patch_carries_the_three_columns_and_the_audience() -> None:
    """The entry states urn, revision and status, plus the audience a pending action has.

    The control mark and the assignee are the optional columns: only a Run control line
    fills the first, and only a pending action addressed to one principal the second.
    """
    entry = patches_for_event(_pause_envelope())[0].entries[0]
    assert set(entry.model_dump()) == {
        "key",
        "urn",
        "collection",
        "revision",
        "status",
        "control",
        "assignee_ref",
    }
    assert entry.control is None
    assert entry.assignee_ref is None


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


def test_the_applied_patch_is_counted_for_its_audience() -> None:
    """The arriving question is in every principal's count when it names no assignee."""
    seam = _seam_holding(_projection(ATTENTION_ROUTE, document={}))
    patch = next(
        p
        for p in patches_for_event(_pause_envelope(status="WAITING"))
        if ATTENTION_ROUTE in p.routes
    )
    asyncio.run(seam.apply_patch(patch))
    assert seam.projection is not None
    register = build_register_view(seam.projection)
    assert register.withheld == ()
    assert attention_mine(register, principal="OP-0001").value == "1"


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


def test_the_budget_frame_prints_the_reading_it_read() -> None:
    """Every part of the reading reaches the frame, the unknown token included."""
    rows, _session = _frame(_register(COST_CEILING_ROUTE))
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
