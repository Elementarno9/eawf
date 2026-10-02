"""The consequence card on a linked console: opened from the menu, confirmed, reconciled.

The card itself -- what it previews, the tokens it is bound to, and the result rows the
daemon's answer fills -- is :mod:`eawf.surfaces.tui.console.cards`. This module is the
console's side of it: the lifecycle verbs the action menu offers for the selection, the
keys that open, confirm, cancel and reconcile a card, and the selection keys.

A card is confirmed only at the tokens it was built against: a target that moved before
confirmation reloads the card at what it reads as now, and the old authorization is
withdrawn, never re-targeted.

A bulk selection is marked with Space, a whole register with the action menu's ``*``, and
cleared with ``,``; the action menu then offers the verbs of the selection's entity, and
one card previews the whole selection by identifier. A selection confirmed for a verb
the daemon runs in bulk is sent as one bulk operation rather than target by target (see
:mod:`eawf.surfaces.tui.console.bulk`). Repeating a confirmed card at an unchanged
revision is not prompted again: it is sent again under the same operation id, which the
daemon answers with what the first send did. A changed revision always prompts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from types import MappingProxyType
from typing import Final

from eawf.kernel.delivery.bulk import BulkVerb
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, ProjectionRow
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.state.epoch2.consequence import (
    CANONICAL_MUTATIONS,
    MUTATIONS_BY_METHOD,
    CanonicalMutation,
)
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.surfaces.tui.console.action_menu import Availability, MenuVerb
from eawf.surfaces.tui.console.attention import selected_open_row
from eawf.surfaces.tui.console.bulk import BulkRequest
from eawf.surfaces.tui.console.cards import (
    CARD,
    NO_LINK_REASON,
    NO_STAMP,
    RECONCILABLE,
    Card,
    Gate,
    GateKind,
    Item,
    Result,
    answer_card,
    campaign_card,
    control_card,
    dispatch_card,
    dispatch_token,
    gate,
    lifecycle_card,
    notice_card,
    setting_card,
    setting_token,
    stamp,
    status_of,
    target_card,
)
from eawf.surfaces.tui.console.keymap import allowlist
from eawf.surfaces.tui.console.live_reads import held_queue
from eawf.surfaces.tui.console.navigation import Ctx, leave_overlay, open_overlay
from eawf.surfaces.tui.console.notices import notice_of
from eawf.surfaces.tui.console.operations import (
    ATTENTION_ROUTE,
    CAMPAIGN_ROUTE,
    DISPATCH_KINDS,
    DROP_CAMPAIGN_VERB,
    RUN_CONTROLS,
    RUN_KINDS,
    SAME_VERB,
    LifecycleRequest,
    SettingRequest,
)
from eawf.surfaces.tui.console.session import Session

logger = logging.getLogger(__name__)


#: The menu-local letter that marks every row of the focused register: the one place a
#: shifted key may act, because a menu letter carries no case meaning.
MARK_ALL: Final = "*"
MARK_ALL_VERB: Final = "select all shown"
#: What a selection key says where the cursor names no record a bulk verb can act on.
NOTHING_TO_MARK: Final = "nothing under the cursor can be marked here"

#: The menu letter each lifecycle verb runs, menu-local to the drawer it is listed in.
NATIVE_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "domain.track.retire": "r",
        "domain.milestone.activate": "t",
        "domain.milestone.open_review": "o",
        "domain.milestone.accept": "a",
        "domain.milestone.cancel": "x",
        "domain.batch.activate": "t",
        "domain.batch.ready": "d",
        "domain.batch.merge": "m",
        "domain.batch.observe_merge": "b",
        "domain.batch.complete": "o",
        "domain.task.promote": "m",
        "domain.task.demote": "o",
        "domain.task.claim": "l",
        "domain.task.release": "r",
        "domain.task.start": "s",
        "domain.task.ready": "w",
        "domain.task.complete": "c",
        "domain.run.start": "a",
        "domain.run.finish": "o",
        "domain.run.fail": "x",
    }
)

#: The routes whose menu offers an entity's lifecycle verbs: where that entity is the
#: subject or the register, not every route that happens to list it.
ENTITY_ROUTES: Final[Mapping[LifecycleEntity, frozenset[str]]] = MappingProxyType(
    {
        LifecycleEntity.TRACK: frozenset({"scope.home", "track"}),
        LifecycleEntity.MILESTONE: frozenset({"scope.home", "track", "milestone"}),
        LifecycleEntity.DELIVERY_BATCH: frozenset({"scope.home", "milestone", "batch.detail"}),
        LifecycleEntity.TASK: frozenset({"task.detail", "backlog"}),
        LifecycleEntity.RUN: frozenset({"activity", "run.detail"}),
    }
)

_ENTITIES: Final[Mapping[str, LifecycleEntity]] = MappingProxyType(
    {entity.value: entity for entity in LifecycleEntity}
)


def entity_of(row: ProjectionRow) -> LifecycleEntity | None:
    """Return the lifecycle entity a row is, or ``None`` for any other record."""
    return _ENTITIES.get(row.collection.value)


def _ended(row: ProjectionRow) -> bool:
    """Return whether ``row`` is a lifecycle record whose stated status has no way out."""
    entity, status = entity_of(row), status_of(row)
    return entity is not None and status is not None and status in TERMINAL_STATUSES[entity]


def verbs_for(entity: LifecycleEntity) -> tuple[CanonicalMutation, ...]:
    """Return ``entity``'s lifecycle verbs, in the daemon's registration order."""
    return tuple(mutation for mutation in CANONICAL_MUTATIONS if mutation.entity is entity)


def _row(rows: Sequence[ProjectionRow], key: str | None) -> ProjectionRow | None:
    return next((row for row in rows if row.key == key), None) if key else None


def cursor_row(session: Session, rows: Sequence[ProjectionRow]) -> ProjectionRow | None:
    """Return the record the cursor names: the route's subject, else the selected row."""
    return _row(rows, session.subj_id) or _row(rows, session.sel_id)


def selection(session: Session, rows: Sequence[ProjectionRow]) -> tuple[ProjectionRow, ...]:
    """Return what a verb acts on: the marked rows, else the record under the cursor."""
    marked = tuple(row for key in session.marked if (row := _row(rows, key)) is not None)
    if marked:
        return marked
    row = cursor_row(session, rows)
    return (row,) if row is not None else ()


def menu_entity(session: Session, rows: Sequence[ProjectionRow]) -> LifecycleEntity | None:
    """Return the entity whose verbs the route's menu offers now, or ``None``.

    The selection must be one entity and the route one where that entity's verbs belong,
    and not every record in it may have finished: a record whose lifecycle has ended has no
    verb left to offer.
    """
    selected = selection(session, rows)
    entities = {entity_of(row) for row in selected}
    if len(entities) != 1 or all(_ended(row) for row in selected):
        return None
    entity = entities.pop()
    if entity is None or session.route not in ENTITY_ROUTES.get(entity, frozenset()):
        return None
    return entity


# ---------- the action menu ----------


def lifecycle_verbs(
    session: Session, rows: Sequence[ProjectionRow], decided: Gate
) -> tuple[MenuVerb, ...]:
    """Return the lifecycle verbs the menu offers for the selection, none under transport loss.

    Args:
        session: The session whose route and selection are read.
        rows: The rows the link holds.
        decided: The write gate's decision.
    """
    entity = menu_entity(session, rows)
    if entity is None or decided.kind is GateKind.TRANSPORT:
        return ()
    mark_all = MenuVerb(key=MARK_ALL, verb=MARK_ALL_VERB, available=True)
    return (
        mark_all,
        *(
            MenuVerb(
                key=NATIVE_KEYS[mutation.method],
                verb=mutation.action,
                available=True,
                authority=mutation.authority,
            )
            for mutation in verbs_for(entity)
        ),
    )


def verb_check(
    session: Session, rows: Sequence[ProjectionRow], decided: Gate, verb: MenuVerb
) -> Availability:
    """Return whether one lifecycle verb in the menu can act, with its live reason.

    A gate refusal names the gate's reason; otherwise the verb is judged on the targets:
    it is refused only when every target refuses, and the reason is the first target's.
    """
    if verb.key == MARK_ALL:
        return Availability(True)
    if decided.kind is not GateKind.OPEN:
        return Availability(False, decided.reason)
    mutation = native_mutation(session, rows, verb.key)
    if mutation is None:
        return Availability(False, "no lifecycle verb has this letter here")
    card = lifecycle_card(mutation, selection(session, rows), principal="", now=0.0)
    if card.sendable:
        return Availability(True)
    return Availability(False, card.items[0].why)


def native_mutation(
    session: Session, rows: Sequence[ProjectionRow], key: str
) -> CanonicalMutation | None:
    """Return the lifecycle verb ``key`` runs in the menu for the selection, if any."""
    entity = menu_entity(session, rows)
    if entity is None:
        return None
    return next((m for m in verbs_for(entity) if NATIVE_KEYS[m.method] == key), None)


def chrome_kept(verbs: Sequence[MenuVerb], native: Sequence[MenuVerb]) -> tuple[MenuVerb, ...]:
    """Return the chrome verbs a linked menu keeps: those no lifecycle verb replaces."""
    letters = {verb.key for verb in native}
    same = {*SAME_VERB, MARK_ALL_VERB}
    return tuple(verb for verb in verbs if not (verb.key in letters and verb.verb in same))


# ---------- the keys ----------


def principal_of(ctx: Ctx) -> str:
    """Return who the console acts as, as far as the card can say."""
    return "" if ctx.principal_refusal else "you"


def _gate(ctx: Ctx, verb: str, *, needs_principal: bool = True) -> Gate:
    return gate(
        ctx.s,
        ctx.fixture,
        verb=verb,
        linked=ctx.send is not None,
        principal_refusal=ctx.principal_refusal,
        needs_principal=needs_principal,
    )


def menu_key(ctx: Ctx, k: str) -> bool:
    """Run the lifecycle verb letter ``k`` names in the open action menu.

    Returns:
        Whether a lifecycle verb claimed the letter; ``False`` passes it to the chrome menu.
    """
    s = ctx.s
    if ctx.fixture.prototype:
        return False
    if k == MARK_ALL and menu_entity(s, ctx.rows) is not None:
        if select_key(ctx, k):
            leave_overlay(s)
        else:
            # the menu stays open: nothing was marked, and closing it would say otherwise
            ctx.log(k, NOTHING_TO_MARK)
        return True
    mutation = native_mutation(s, ctx.rows, k)
    if mutation is None:
        return False
    decided = _gate(ctx, mutation.action)
    if decided.kind is GateKind.TRANSPORT:
        ctx.log(k, f"{mutation.action} is not offered · {decided.reason}")
        return True
    if decided.kind is GateKind.REFUSED:
        ctx.log(k, f"refused: {decided.reason}")
        return True
    open_lifecycle(ctx, mutation)
    return True


def _repeat(s: Session, mutation: CanonicalMutation, row: ProjectionRow) -> str | None:
    """Return the operation id a confirmed card holds for ``row`` at its unchanged revision."""
    held = s.confirmed.get(f"{mutation.method} {row.key}")
    if held is None:
        return None
    revision, operation_id = held
    return operation_id if revision == int(row.revision) else None


def open_lifecycle(ctx: Ctx, mutation: CanonicalMutation) -> None:
    """Open the card previewing ``mutation`` on the selection, or repeat a confirmed one.

    A single target confirmed before at this same revision is not prompted again: the
    card is sent again under the operation id it was confirmed with, which the daemon
    answers with what the first send did.
    """
    s = ctx.s
    targets = selection(s, ctx.rows)
    reuse = {row.key: held for row in targets if (held := _repeat(s, mutation, row)) is not None}
    card = lifecycle_card(
        mutation, targets, principal=principal_of(ctx), now=ctx.clock.now(), reuse=reuse
    )
    s.mutation = card
    open_overlay(s, CARD, subject=targets[0].key if len(targets) == 1 else None)
    if len(targets) == 1 and reuse:
        ctx.log(".", f"{mutation.action} {targets[0].key} already confirmed at this revision")
        confirm(ctx)
        return
    ctx.log(".", f"{mutation.action} · {len(targets)} {mutation.entity.value} → consequence")


def open_setting(
    ctx: Ctx,
    request: SettingRequest,
    *,
    effect: str,
    token: str,
    changes: tuple[str, ...] = (),
) -> None:
    """Open the card previewing one settings edit, or say why no request can leave.

    ``changes`` is the per-member difference of a whole list or mapping written at once,
    drawn on the card in place of the value.
    """
    key = "x" if request.unset else "Enter"
    decided = _gate(ctx, f"set {request.target}", needs_principal=False)
    if decided.kind is GateKind.TRANSPORT:
        ctx.log(key, f"no daemon link · nothing was written · {decided.reason}")
        return
    if decided.kind is GateKind.REFUSED:
        ctx.log(key, f"refused: {decided.reason}")
        return
    ctx.s.mutation = setting_card(
        request, effect=effect, token=token, now=ctx.clock.now(), changes=changes
    )
    open_overlay(ctx.s, CARD, subject=request.target)
    ctx.log(key, f"{request.target} · {effect} → consequence preview")


def open_target(ctx: Ctx, key: str, day: date) -> None:
    """Open the card previewing ``day`` as the held Milestone ``key``'s target, or say why not.

    Args:
        ctx: The keystroke's context.
        key: The Milestone's public key.
        day: The calendar day the operator typed.
    """
    decided = _gate(ctx, "propose date")
    if decided.kind is not GateKind.OPEN:
        ctx.log("Enter", f"propose date refused · {decided.reason}")
        return
    row = _row(ctx.rows, key)
    if row is None:
        ctx.log("Enter", f"{key} is in no projection the console holds · nothing to date")
        return
    ctx.s.mutation = target_card(row, day, principal=principal_of(ctx), now=ctx.clock.now())
    open_overlay(ctx.s, CARD, subject=key)
    ctx.log("Enter", f"propose date {key} {day.isoformat()} → consequence preview")


def adopt(ctx: Ctx) -> None:
    """Build the card a consequence overlay opened elsewhere needs, from what the link holds.

    An attention verb, a Run control and a Campaign's drop open the consequence overlay by
    name; on a linked console the card they preview is built here, from the held pending
    action, Run or Campaign, so the card states the revision the write will be addressed
    at. Nothing is built for a console holding the prototype registers, whose card is the
    golden one.
    """
    s = ctx.s
    if s.overlay != CARD:
        s.mutation = None
        return
    if s.mutation is not None or ctx.fixture.prototype:
        return
    now = ctx.clock.now()
    target = s.c_target
    notice = notice_of(ctx.notices, s.ov_subject) if s.route == ATTENTION_ROUTE else None
    if target is None and notice is not None:
        s.mutation = notice_card(
            notice, s.verb, principal=principal_of(ctx), now=now, wall=ctx.clock.wall()
        )
        return
    if target is None and s.route == ATTENTION_ROUTE and ctx.attention is not None:
        row = selected_open_row(s, ctx.attention)
        if row is not None:
            s.mutation = answer_card(
                row,
                s.verb or "a",
                principal=principal_of(ctx),
                now=now,
                wall=ctx.clock.wall(),
                rows=ctx.attention.rows,
            )
        return
    if target is not None:
        s.mutation = _target_card(ctx, target, now)


def _target_card(ctx: Ctx, target: Mapping[str, str], now: float) -> Card | None:
    """Return the card a menu verb's target previews, or ``None`` when no held record is it.

    The target is a Run control, a request of the tree's one dispatch queue, or a
    Campaign's drop; the queue is no row, every other target is the held row it names.
    """
    kind, verb, principal = target.get("kind"), target["verb"], principal_of(ctx)
    if kind in DISPATCH_KINDS:
        return dispatch_card(ctx.live, target, principal=principal, now=now)
    row = _row(ctx.rows, target["id"])
    if row is not None and kind in RUN_KINDS and verb in RUN_CONTROLS:
        return control_card(row, target, principal=principal, now=now)
    if row is not None and (kind, verb) == (CAMPAIGN_ROUTE, DROP_CAMPAIGN_VERB):
        return campaign_card(row, target, principal=principal, now=now)
    return None


def _current_token(ctx: Ctx, card: Card, item: Item) -> str | None:
    """Return what the target reads as now, in the card's stale-token terms."""
    if card.kind == "setting":
        settings = ctx.settings
        return setting_token(settings.leaf(item.key)) if settings is not None else None
    if card.kind == "notice":
        notice = notice_of(ctx.notices, item.key)
        return str(notice.revision) if notice is not None else None
    if card.kind == "dispatch":
        return dispatch_token(held_queue(ctx.live))
    if card.kind == "answer" and ctx.attention is not None:
        row = _row(ctx.attention.rows, item.key)
    else:
        row = _row(ctx.rows, item.key)
    return str(row.revision) if row is not None else None


def _rebuilt(ctx: Ctx, card: Card) -> Card | None:
    """Return ``card`` built again from what the link holds now; ``None`` when nothing is held."""
    now, principal = ctx.clock.now(), principal_of(ctx)
    key, target = card.items[0].key, ctx.s.c_target
    if card.kind == "lifecycle":
        rows = tuple(row for item in card.items if (row := _row(ctx.rows, item.key)) is not None)
        mutation = MUTATIONS_BY_METHOD[card.origin]
        return lifecycle_card(mutation, rows, principal=principal, now=now) if rows else None
    if card.kind == "answer" and ctx.attention is not None:
        row = _row(ctx.attention.rows, key)
        wall, held = ctx.clock.wall(), ctx.attention.rows
        return (
            answer_card(row, card.origin, principal=principal, now=now, wall=wall, rows=held)
            if row is not None
            else None
        )
    if card.kind == "notice":
        notice = notice_of(ctx.notices, key)
        wall = ctx.clock.wall()
        return (
            notice_card(notice, card.origin, principal=principal, now=now, wall=wall)
            if notice is not None
            else None
        )
    if card.kind == "control" and target is not None:
        row = _row(ctx.rows, key)
        return control_card(row, target, principal=principal, now=now) if row is not None else None
    if card.kind == "dispatch" and target is not None:
        return dispatch_card(ctx.live, target, principal=principal, now=now)
    if card.kind == "target":
        row = _row(ctx.rows, key)
        day = date.fromisoformat(card.origin)
        return target_card(row, day, principal=principal, now=now) if row else None
    if card.kind == "campaign" and target is not None:
        row = _row(ctx.rows, key)
        return campaign_card(row, target, principal=principal, now=now) if row else None
    return None


def _reload(ctx: Ctx, card: Card, moved: Sequence[tuple[Item, str | None]]) -> None:
    """Rebuild the card at the targets' current revisions; the old authorization is gone."""
    s = ctx.s
    said = ", ".join(f"{item.key} {item.stale_token} → {now or 'gone'}" for item, now in moved)
    note = f"reloaded · {said} · the authorization was withdrawn"
    rebuilt = _rebuilt(ctx, card)
    if rebuilt is None:
        s.mutation = None
        leave_overlay(s)
        ctx.log("Enter", f"{note} · nothing is held to preview again")
        return
    s.mutation = replace(rebuilt, note=note)
    ctx.log("Enter", note)


def _requested(card: Card, item: Item, now: float) -> Result:
    return Result(key=item.key, requested=stamp(card, now))


def confirm(ctx: Ctx) -> None:
    """Confirm the open card: reload it if a target moved, else send every sendable target.

    A target refused on the card is recorded refused and never sent. Each sent target
    keeps its own row, which only the daemon's answer settles.
    """
    s = ctx.s
    card = s.mutation
    if not isinstance(card, Card):
        return
    if card.results:
        ctx.log("Enter", "already confirmed · the rows below are the daemon's answers")
        return
    decided = _gate(ctx, card.action, needs_principal=card.kind != "setting")
    if decided.kind is not GateKind.OPEN:
        ctx.log("Enter", f"{card.action} refused before sending · {decided.reason}")
        return
    # a target refused on the card is never sent, so whether it moved decides nothing
    moved = [
        (item, current)
        for item in card.items
        if item.request is not None
        and (current := _current_token(ctx, card, item)) != item.stale_token
    ]
    if moved:
        _reload(ctx, card, moved)
        return
    if not card.sendable:
        ctx.log("Enter", f"{card.action} refused · {card.items[0].why}")
        return
    now = ctx.clock.now()
    if card.bulk_verb is not None:
        _confirm_bulk(ctx, card, card.bulk_verb, now)
        return
    results: list[Result] = []
    for item in card.items:
        if item.refusal is not None:
            results.append(
                Result(
                    key=item.key,
                    disposition=ControlDisposition.IDLE,
                    detail=f"{item.refusal.code} · {item.refusal.reason}",
                )
            )
            continue
        results.append(_send(ctx, card, item, now))
    s.mutation = replace(card, results=tuple(results))
    sent = sum(1 for row in results if row.requested != NO_STAMP)
    ctx.log("Enter", f"{card.action} · {sent} of {len(results)} sent · waiting for the daemon")


def _bulk_request(card: Card, verb: BulkVerb, *, reconcile: bool = False) -> BulkRequest:
    """Return the one bulk operation a card's sendable targets are sent as.

    The operation is named by the first sendable target's id, which the card keeps, so a
    reconcile addresses the operation the confirmation opened.
    """
    moves = [item.request for item in card.sendable]
    assert all(isinstance(move, LifecycleRequest) for move in moves), "lifecycle moves only"
    lifecycle = [move for move in moves if isinstance(move, LifecycleRequest)]
    return BulkRequest(
        verb=verb,
        targets=tuple(move.target for move in lifecycle),
        revisions={move.target: move.revision for move in lifecycle},
        operation_id=lifecycle[0].operation_id,
        reconcile=reconcile,
    )


def _confirm_bulk(ctx: Ctx, card: Card, verb: BulkVerb, now: float) -> None:
    """Send a confirmed selection as one bulk operation, one row per target.

    A target refused on the card is recorded refused and never sent; every sent target's
    row waits for its own item result.
    """
    sent = ctx.dispatch_write(_bulk_request(card, verb))
    results: list[Result] = []
    for item in card.items:
        if item.request is not None and sent:
            results.append(_requested(card, item, now))
            continue
        why = item.refusal
        detail = f"{why.code} · {why.reason}" if why else NO_LINK_REASON
        results.append(Result(key=item.key, disposition=ControlDisposition.IDLE, detail=detail))
    ctx.s.mutation = replace(card, results=tuple(results))
    count = len(card.sendable) if sent else 0
    ctx.log(
        "Enter",
        f"{card.action} · {count} of {len(results)} sent as one operation · waiting for the daemon",
    )


def _send(ctx: Ctx, card: Card, item: Item, now: float) -> Result:
    """Hand one target's request to the link and return its row, requested or not sent."""
    request = item.request
    assert request is not None, "only sendable targets are sent"
    if isinstance(request, LifecycleRequest) and item.clock_fields:
        at = ctx.clock.wall().isoformat()
        request = replace(request, updates=dict.fromkeys(item.clock_fields, at))
    if not ctx.dispatch_write(request):
        return Result(key=item.key, disposition=ControlDisposition.IDLE, detail=NO_LINK_REASON)
    if isinstance(request, LifecycleRequest):
        ctx.s.confirmed = {
            **ctx.s.confirmed,
            f"{request.method} {request.target}": (request.revision, request.operation_id),
        }
    return _requested(card, item, now)


def reconcile(ctx: Ctx) -> None:
    """Ask again for the selected unknown row, under the operation id it was sent with."""
    s = ctx.s
    card = s.mutation
    if not isinstance(card, Card) or not card.results:
        ctx.noop("n")
        return
    row = card.results[min(card.sel, len(card.results) - 1)]
    if row.disposition not in RECONCILABLE:
        ctx.log("n", f"{row.key} is {row.disposition} · only an unknown row is reconciled")
        return
    if card.bulk_verb is not None:
        if not ctx.dispatch_write(_bulk_request(card, card.bulk_verb, reconcile=True)):
            ctx.log("n", f"{row.key} · {NO_LINK_REASON}")
            return
        ctx.log("n", f"reconcile {row.key} · every unsettled target asked again")
        return
    item = next(item for item in card.items if item.key == row.key)
    if item.request is None or not ctx.dispatch_write(item.request):
        ctx.log("n", f"{row.key} · {NO_LINK_REASON}")
        return
    ctx.log("n", f"reconcile {row.key} · asked again under its operation id")


def card_key(ctx: Ctx, k: str) -> None:
    """Handle a key on the linked card: confirm, cancel, walk the results or reconcile."""
    s = ctx.s
    card = s.mutation
    if not isinstance(card, Card) or k not in allowlist(s, ctx.fixture):
        ctx.noop(k)
        return
    if k == "Enter":
        confirm(ctx)
    elif k == "Escape":
        leave_overlay(s)
        unknown = sum(1 for row in card.results if row.disposition in RECONCILABLE)
        if not card.results:
            s.mutation = None
            ctx.log("Esc", "cancelled — nothing happened")
        elif unknown:
            ctx.log("Esc", f"back · {unknown} unknown rows stay unknown until reconcile answers")
        else:
            ctx.log("Esc", "back")
    elif k in ("ArrowUp", "ArrowDown", "j", "k") and card.results:
        step = 1 if k in ("ArrowDown", "j") else -1
        s.mutation = replace(card, sel=(card.sel + step) % len(card.results))
        ctx.log(k, "result row")
    elif k == "n" and card.results:
        reconcile(ctx)
    else:
        ctx.noop(k)


# ---------- the selection ----------


def select(ctx: Ctx, k: str, pane: bool) -> None:
    """Run a selection key from the route's key path, or say why nothing was marked.

    Help teaches the selection keys on a linked console's record lists, so where the row
    under the cursor cannot be marked the key answers rather than doing nothing.
    """
    if pane:
        ctx.noop(k)
    elif not select_key(ctx, k):
        if ctx.fixture.prototype:
            ctx.noop(k)
        else:
            ctx.log(k, NOTHING_TO_MARK)


def select_key(ctx: Ctx, k: str) -> bool:
    """Mark the cursor row, a whole register, or clear the marks, on a linked console.

    Returns:
        Whether the key was claimed; the selection keys act only where the route lists
        lifecycle records the link holds.
    """
    if k not in (" ", "*", ",") or ctx.fixture.prototype:
        return False
    s = ctx.s
    row = cursor_row(s, ctx.rows) if s.subj_id is None else _row(ctx.rows, s.sel_id)
    entity = entity_of(row) if row is not None else None
    if row is None or entity is None or s.route not in ENTITY_ROUTES.get(entity, frozenset()):
        return False
    if k == ",":
        s.marked = []
        ctx.log(",", "selection cleared")
    elif k == " ":
        marked = [key for key in s.marked if key != row.key]
        s.marked = marked if row.key in s.marked else [*marked, row.key]
        ctx.log("space", f"{row.key} · {len(s.marked)} selected")
    else:
        bound = {collection.value for collection in ROUTE_COLLECTIONS.get(s.route, ())}
        s.marked = [
            each.key
            for each in ctx.rows
            if each.collection.value in bound and entity_of(each) is entity
        ]
        note = f"every {entity.value} in this register · {len(s.marked)} selected"
        # the menu closes as the rows are marked, so the frame alone would not say so
        ctx.notify(note, MARK_ALL_VERB)
        ctx.log("*", note)
    return True


__all__ = [
    "NATIVE_KEYS",
    "adopt",
    "card_key",
    "confirm",
    "lifecycle_verbs",
    "menu_key",
    "select_key",
    "verb_check",
]
