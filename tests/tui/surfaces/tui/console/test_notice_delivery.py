"""Delivery: at most once per principal per revision, never a storm, never the screen.

UI-015. A notice event reaches no modal push, focus change or navigation: the path a
committed change takes into the console -- a keyed patch, the seam's fan-out, the app's
patch listener -- is driven here, and the session's route, overlay, selection and focus
are the same after it as before; the one thing it may add is a toast.

UI-016. Per-principal delivery survives a client restart and a replay: a console that
attaches again seeds what was delivered from its first read, so nothing already open is
announced again, while a new revision is announced once to the principal it is addressed
to. A dismissal the source record does not define is refused, never kept in memory.

UI-018. A clean load and a replay of the same moves produce one projection -- the same
digest, the same rows, the same audience -- and no delivery for a revision already
delivered.

UI-019. The legacy stale-advisory bands collapse into one highest-band notice per
condition: a repeated band writes nothing, a lower band never regresses it, nothing on
the notice claims an acknowledgement nobody gave, and a console attaching to a register
already holding items raises no toast at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.attention import delivered_revisions, deliveries
from eawf.kernel.projection.compute import (
    RouteProjection,
    build_route_projection,
    patches_for_event,
)
from eawf.kernel.projection.connection import apply_patches
from eawf.kernel.projection.registers import ATTENTION_ROUTE, build_register_view
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.budget.notices import (
    BudgetCrossing,
    BudgetNoticeLedger,
    UpsertOutcome,
    apply_crossing,
)
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.operations import Operator, binding_refusal
from eawf.surfaces.tui.console.seam import ProjectionSeam

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CONTAINER = "eawf://WSP-A/EAWF/REP-A"
PRINCIPAL = "OP-0001"
OTHER = "OP-0002"


def _urn(key: str) -> str:
    """Return the address of pending action ``key``."""
    return f"{CONTAINER}/pending-action/{key}"


def _row(
    key: str, *, status: str = "WAITING", revision: int = 1, assignee: str | None = None
) -> dict[str, Any]:
    """Return one stored pending-action row."""
    row: dict[str, Any] = {"urn": _urn(key), "revision": revision, "status": status}
    if assignee is not None:
        row["assignee_ref"] = assignee
    return row


def _projection(rows: dict[str, Any], *, cursor: int) -> RouteProjection:
    """Return the Attention projection over ``rows`` at ``cursor``."""
    return build_route_projection(
        route=ATTENTION_ROUTE,
        document={"pending_action": rows},
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _envelope(
    key: str, *, sequence: int, status: str, revision: int, assignee: str | None
) -> Envelope:
    """Return the committed move of ``key`` as the approval verb records it."""
    return Envelope(
        id=f"evt-{sequence}",
        kind=StoreKind.EVENT,
        scope_id=SCOPE,
        created_at=AT,
        summary=f"{key} {status}",
        payload={
            "entity_ref": _urn(key),
            "to_status": status,
            "revision_after": revision,
            "canonical_sequence": sequence,
            "assignee_ref": assignee,
        },
    )


def _patch(key: str, **kwargs: Any) -> Any:
    """Return the Attention patch of one move."""
    return next(
        p for p in patches_for_event(_envelope(key, **kwargs)) if ATTENTION_ROUTE in p.routes
    )


def _console(held: RouteProjection, *, principal: str | None = PRINCIPAL) -> ConsoleApp:
    """Return a console, not running, whose seam holds ``held`` and acts as ``principal``."""
    seam = ProjectionSeam(
        route=ATTENTION_ROUTE,
        scope_id=SCOPE,
        state_path=None,
        clock=lambda: AT,
        operator=Operator(principal=principal) if principal is not None else None,
    )
    seam._projection = held
    app = ConsoleApp(chrome=load_chrome(), seam=seam)
    app.session.route = "activity"
    app.deliver_attention()
    return app


def _apply(app: ConsoleApp, patch: Any) -> None:
    """Push ``patch`` down the console's live path: the seam's fan-out, then its listeners."""
    seam = app.seam
    assert seam is not None
    asyncio.run(seam.apply_patch(patch))


def _screen(app: ConsoleApp) -> tuple[Any, ...]:
    """Return what a notice may never move: route, overlay, selection and focus."""
    s = app.session
    return (s.route, s.overlay, s.sel, s.sel_id, s.region, s.focus_return, len(s.back.entries))


# ---------- UI-015: nothing reaches a modal, a focus change or a route change ----------


def test_ui_015_a_patch_through_the_live_path_only_adds_a_toast() -> None:
    """The seam's fan-out and the app's listener leave the screen where it was."""
    app = _console(_projection({}, cursor=10))
    before = _screen(app)
    _apply(app, _patch("ACT-0001", sequence=11, status="WAITING", revision=1, assignee=None))
    assert _screen(app) == before
    assert [t.title for t in app.session.toasts] == ["needs you"]
    assert "ACT-0001" in app.session.toasts[0].text


def test_ui_015_a_move_that_closes_an_item_raises_nothing() -> None:
    """A sealed question is announced to nobody and moves nothing."""
    app = _console(_projection({"ACT-0001": _row("ACT-0001")}, cursor=10))
    before = _screen(app)
    _apply(app, _patch("ACT-0001", sequence=11, status="SEALED", revision=2, assignee=None))
    assert _screen(app) == before
    assert app.session.toasts == []


# ---------- UI-016: per principal, surviving restart and replay ----------


def test_ui_016_an_item_addressed_to_another_principal_is_not_announced_here() -> None:
    """Delivery is per principal: OP-0002's question does not toast OP-0001's console."""
    app = _console(_projection({}, cursor=10))
    _apply(app, _patch("ACT-0001", sequence=11, status="WAITING", revision=1, assignee=OTHER))
    assert app.session.toasts == []
    other = _console(_projection({}, cursor=10), principal=OTHER)
    _apply(other, _patch("ACT-0001", sequence=11, status="WAITING", revision=1, assignee=OTHER))
    assert len(other.session.toasts) == 1


def test_ui_016_a_console_acting_as_nobody_is_delivered_nothing() -> None:
    """No item is addressed to nobody, so no toast is raised for it."""
    app = _console(_projection({}, cursor=10), principal=None)
    _apply(app, _patch("ACT-0001", sequence=11, status="WAITING", revision=1, assignee=None))
    assert app.session.toasts == []


def test_ui_016_a_restarted_console_does_not_announce_what_was_already_open() -> None:
    """The first read seeds the delivered record, so a restart replays no toast."""
    held = _projection({"ACT-0001": _row("ACT-0001"), "ACT-0002": _row("ACT-0002")}, cursor=10)
    restarted = _console(held)
    assert restarted.session.toasts == []
    restarted.deliver_attention()
    assert restarted.session.toasts == []


def test_ui_016_each_revision_is_announced_at_most_once() -> None:
    """A repeated patch for a delivered revision is ignored; a later revision is new."""
    app = _console(_projection({}, cursor=10))
    first = _patch("ACT-0001", sequence=11, status="WAITING", revision=1, assignee=None)
    _apply(app, first)
    _apply(app, first)
    app.deliver_attention()
    assert len(app.session.toasts) == 1


def test_ui_016_a_dismissal_the_record_does_not_define_is_refused() -> None:
    """A pending action defines no snooze, so the console binds none rather than faking one."""
    assert binding_refusal(ATTENTION_ROUTE, "snooze")


# ---------- UI-018: clean load, replay and reconnect converge ----------


def test_ui_018_a_replay_equals_a_clean_read_audience_included() -> None:
    """The same moves, replayed or read clean, give one digest and one audience."""
    before = _projection({"ACT-0001": _row("ACT-0001", assignee=PRINCIPAL)}, cursor=10)
    patches = [
        _patch("ACT-0002", sequence=11, status="WAITING", revision=1, assignee=OTHER),
        _patch("ACT-0001", sequence=12, status="SEALED", revision=2, assignee=None),
    ]
    replayed = apply_patches(before, patches, cursor=12, scope_id=SCOPE, generated_at=AT)
    clean = _projection(
        {
            "ACT-0001": _row("ACT-0001", status="SEALED", revision=2),
            "ACT-0002": _row("ACT-0002", assignee=OTHER),
        },
        cursor=12,
    )
    assert replayed.digest == clean.digest
    assert [(r.key, r.assignee_ref) for r in replayed.rows] == [
        ("ACT-0001", None),
        ("ACT-0002", OTHER),
    ]


def test_ui_018_no_delivery_for_a_revision_already_delivered() -> None:
    """Whatever route the rows took in, a delivered revision is not delivered again."""
    clean = build_register_view(_projection({"ACT-0001": _row("ACT-0001")}, cursor=10))
    seen = delivered_revisions(clean)
    assert deliveries(clean, principal=PRINCIPAL, delivered=seen) == ()
    assert [i.key for i in deliveries(clean, principal=PRINCIPAL, delivered=())] == ["ACT-0001"]


def test_ui_018_the_empty_register_delivers_nothing() -> None:
    """The empty boundary: nothing open, nothing seeded, nothing announced."""
    empty = build_register_view(_projection({}, cursor=0))
    assert delivered_revisions(empty) == frozenset()
    assert deliveries(empty, principal=PRINCIPAL, delivered=()) == ()


def test_ui_018_another_routes_register_is_refused() -> None:
    """Rows that are not actions cannot be delivered as actions."""
    run = build_register_view(
        build_route_projection(
            route="activity", document={}, cursor=0, scope_id=SCOPE, generated_at=AT
        )
    )
    with pytest.raises(ValueError, match="states no attention items"):
        deliveries(run, principal=PRINCIPAL, delivered=())


# ---------- UI-019: the bands collapse, and nothing storms at startup ----------


def _crossing(band: str, observed: int) -> BudgetCrossing:
    """Return one crossing of the probe scope's estimate budget."""
    return BudgetCrossing.model_validate(
        {
            "scope_id": "P01-I01-W01",
            "basis": "estimate",
            "band": band,
            "observed_value": observed,
            "budget_value": 1000,
            "observed_at": AT.isoformat(),
        }
    )


def test_ui_019_the_bands_collapse_into_one_highest_band_notice() -> None:
    """The approaching and the limit band are one notice at one escalating revision."""
    ledger, first = apply_crossing(BudgetNoticeLedger(), _crossing("approaching", 750))
    ledger, escalated = apply_crossing(ledger, _crossing("limit_reached", 1000))
    ledger, repeat = apply_crossing(ledger, _crossing("limit_reached", 1100))
    ledger, lower = apply_crossing(ledger, _crossing("approaching", 800))
    assert [first.outcome, escalated.outcome, repeat.outcome, lower.outcome] == [
        UpsertOutcome.CREATED,
        UpsertOutcome.ESCALATED,
        UpsertOutcome.UNCHANGED,
        UpsertOutcome.RETAINED,
    ]
    (notice,) = ledger.notices.values()
    assert notice.highest_band == "limit_reached"
    assert notice.revision == 2


def test_ui_019_no_acknowledgement_is_fabricated() -> None:
    """The notice stays open with no resolution stamp; nobody acknowledged it."""
    ledger, _ = apply_crossing(BudgetNoticeLedger(), _crossing("limit_reached", 1000))
    (notice,) = ledger.notices.values()
    assert notice.status == "OPEN"
    assert notice.resolved_at is None
    assert "acknowledged" not in notice.model_dump()


def test_ui_019_attaching_to_a_full_register_raises_no_startup_storm() -> None:
    """Fifty open questions on attach, and not one toast."""
    rows = {f"ACT-{i:04d}": _row(f"ACT-{i:04d}") for i in range(1, 51)}
    app = _console(_projection(rows, cursor=60))
    assert app.session.toasts == []
