"""The consequence card on a linked console: previewed, accepted, then answered for real.

Every canonical mutation a linked console can send goes through one card: a lifecycle move
on a Track, Milestone, Batch, Task or Run, a settings write or unset, an answer to a
pending action, and a Run control. Nothing is sent while the card only previews. The card
is built from the row the link holds -- its status and its revision -- and from the
transition table, so what it says the move will change is what the daemon will judge; a
move the request itself cannot satisfy is refused on the card with the denial's own code
and remediation, and nothing is sent for it.

Once confirmed, each target keeps its own result row: requested, accepted and confirmed
stamps and the outcome it reached, filled from the daemon's answer and never from the request. A
target the daemon never answered reads ``unknown`` until reconcile asks again under the
same operation id; it is never counted as a success or a failure.

The single write gate decides what a verb may do before it is chosen. With no request
able to leave the console -- no link, or a link that lost its transport -- the lifecycle
verbs are removed from the menu, because there is nothing to refuse. With a link that
refuses writes for a reason the operator can act on, the verbs stay listed with that
reason. The two never render alike.

A bulk selection is marked with Space, a whole register with the action menu's ``*``, and
cleared with ``,``; the action menu then offers the verbs of the selection's entity, and
one card previews the whole selection by identifier. Repeating a confirmed card at an
unchanged revision is not prompted again: it is sent again under the same operation id,
which the daemon answers with what the first send did. A changed revision always prompts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, ProjectionRow
from eawf.kernel.projection.settings import SettingsLeaf
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.state.epoch2.consequence import (
    CANONICAL_MUTATIONS,
    MUTATIONS_BY_METHOD,
    CanonicalMutation,
    Refusal,
    consequence,
    if_stale,
)
from eawf.kernel.state.epoch2.transitions import (
    AMBIGUOUS_STATES,
    TERMINAL_STATUSES,
    LifecycleEntity,
)
from eawf.surfaces.tui.console.action_menu import Availability, MenuVerb
from eawf.surfaces.tui.console.attention import VERB, selected_open_row
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.keymap import allowlist
from eawf.surfaces.tui.console.navigation import Ctx, leave_overlay, open_overlay
from eawf.surfaces.tui.console.operations import (
    ANSWER_OPTIONS,
    ATTENTION_ROUTE,
    RUN_CONTROLS,
    RUN_KINDS,
    SAME_VERB,
    AnswerRequest,
    ControlRequest,
    LifecycleRequest,
    OperationResult,
    SettingRequest,
    VerbRequest,
    binding_refusal,
    mint_lifecycle_id,
)
from eawf.surfaces.tui.console.reads import mut_reason, transport_lost, write_refusal
from eawf.surfaces.tui.console.session import Session

logger = logging.getLogger(__name__)

#: The overlay the card draws in.
CARD = "consequence"
#: Why no request can be issued on a console started without a daemon link.
NO_LINK_REASON: Final = "the console holds no daemon link · no request can be issued"
#: The menu-local letter that marks every row of the focused register: the one place a
#: shifted key may act, because a menu letter carries no case meaning.
MARK_ALL: Final = "*"
MARK_ALL_VERB: Final = "select all shown"
#: A stamp that has not happened.
NO_STAMP: Final = "—"

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
        "domain.task.claim": "l",
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
        LifecycleEntity.MILESTONE: frozenset({"scope.home", "milestone"}),
        LifecycleEntity.DELIVERY_BATCH: frozenset({"scope.home", "milestone", "batch.detail"}),
        LifecycleEntity.TASK: frozenset({"task.detail", "backlog"}),
        LifecycleEntity.RUN: frozenset({"activity", "run.detail"}),
    }
)

_ENTITIES: Final[Mapping[str, LifecycleEntity]] = MappingProxyType(
    {entity.value: entity for entity in LifecycleEntity}
)
_ANSWER_NOT: Final = (
    "no lifecycle moves: no run is started, stopped or re-owned",
    "it does not accept the milestone the question is about",
)
_CONTROL_NOT: Final = "the run is not moved by the request itself; it moves only when it answers"


class GateKind(StrEnum):
    """What the one write gate decides for every write before one is chosen."""

    OPEN = "open"
    TRANSPORT = "transport"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class Gate:
    """The gate's decision and the reason the operator reads.

    Attributes:
        kind: Open, transport lost (no request can be issued) or refused with a reason.
        reason: Why it is not open; empty when it is.
    """

    kind: GateKind
    reason: str = ""


def gate(
    session: Session,
    fixture: Fixture,
    *,
    verb: str,
    linked: bool,
    principal_refusal: str = "",
    needs_principal: bool = True,
) -> Gate:
    """Return how the one write gate's refusal renders: removed, explained, or open.

    The reason is always :func:`~eawf.surfaces.tui.console.reads.write_refusal`'s; this
    only tells a state no request can leave from one the operator can act on.

    Args:
        session: The session whose connection value is judged.
        fixture: The chrome the connection value's refusal reason is read from.
        verb: The verb the write gate is asked about.
        linked: Whether the console holds a daemon link a request could leave through.
        principal_refusal: Why every attributed write is refused; empty when it is not.
        needs_principal: Whether the write is attributed to a principal at all; a
            settings write is the daemon's own, under its lock.
    """
    if not linked:
        return Gate(GateKind.TRANSPORT, NO_LINK_REASON)
    refusal = write_refusal(
        session,
        fixture,
        verb=verb,
        principal_refusal=principal_refusal if needs_principal else "",
    )
    if transport_lost(session):
        return Gate(GateKind.TRANSPORT, refusal or mut_reason(session, fixture))
    return Gate(GateKind.REFUSED, refusal) if refusal else Gate(GateKind.OPEN)


Kind = Literal["lifecycle", "setting", "answer", "control"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Item:
    """One target of a card: what it is, where it stands, and what will happen to it.

    Attributes:
        key: The record's public key, or the settings key.
        title: The record's title, when it states one.
        revision: The revision the card is bound to; ``None`` for a settings key, which
            carries none.
        status: The status the record was read in; ``None`` when it was not stated.
        effects: What the write will change on this target.
        not_effects: What it will not change.
        refusal: Why this target is refused before sending; ``None`` when it may go.
        unknown: Why this target may not be able to confirm; empty when it can.
        request: What is sent for it on confirmation; ``None`` when refused.
        stale_token: What the card compares at confirmation to tell a moved target.
        clock_fields: The fields stamped with the wall clock at confirmation.
    """

    key: str
    title: str | None
    revision: int | None
    status: str | None
    effects: tuple[str, ...]
    not_effects: tuple[str, ...]
    refusal: Refusal | None
    unknown: str
    request: VerbRequest | None
    stale_token: str
    clock_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Result:
    """One target's result row after confirmation.

    Attributes:
        key: The target.
        requested: When the request left the console, on the card's clock.
        accepted: When the daemon accepted it.
        confirmed: When the daemon confirmed the outcome.
        answered: When the daemon's answer, of whatever kind, reached the console.
        disposition: Where the target's request stands, in the console's one outcome
            vocabulary; ``idle`` for a target refused on the card, which was never asked.
        detail: What the daemon, or the card, said about it.
        revision: The revision the answer stood at.
        answered_by: The principal whose answer won, when this one was superseded.
    """

    key: str
    requested: str = NO_STAMP
    accepted: str = NO_STAMP
    confirmed: str = NO_STAMP
    answered: str = NO_STAMP
    disposition: ControlDisposition = ControlDisposition.REQUESTING
    detail: str = ""
    revision: int | None = None
    answered_by: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Card:
    """One consequence card: one preview for one or many targets, then their results.

    Attributes:
        kind: Which canonical mutation family the card sends.
        origin: What rebuilds the card: the lifecycle method, the attention verb key, the
            Run control, or the settings key.
        action: The verb as the operator reads it.
        noun: What one target is called, for the count.
        items: The targets, in selection order.
        if_stale: The reload rule, stated before confirmation.
        authority: The authority class the write is taken under, and who acts.
        opened_at: The console clock when the card was built; stamps count from it.
        results: The result rows once confirmed; empty while previewing.
        note: What the card says about itself, such as a reload.
        sel: The result row the cursor is on.
        issuer: The principal the request is issued by; empty where the card names none.
    """

    kind: Kind
    origin: str
    action: str
    noun: str
    items: tuple[Item, ...]
    if_stale: str
    authority: str
    opened_at: float
    results: tuple[Result, ...] = ()
    note: str = ""
    sel: int = 0
    issuer: str = ""

    @property
    def bulk(self) -> bool:
        """Return whether the card previews more than one target."""
        return len(self.items) > 1

    @property
    def sendable(self) -> tuple[Item, ...]:
        """Return the targets confirmation sends: the ones not refused on the card."""
        return tuple(item for item in self.items if item.request is not None)


def status_of(row: ProjectionRow) -> str | None:
    """Return the row's status when the projection states it, else ``None``."""
    field = row.status
    return field.value if field.state is TruthState.KNOWN else None


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


def _item(mutation: CanonicalMutation, row: ProjectionRow, operation_id: str) -> Item:
    """Return one lifecycle target, previewed at the row's own status and revision."""
    status = status_of(row)
    revision = int(row.revision)
    stated = consequence(mutation, key=row.key, revision=revision, status=status)
    refusal = stated.refusal
    if status is None:
        refusal = Refusal(
            code="status_unknown",
            reason=f"{row.key} states no status, so the move cannot be previewed",
            remediation="Wait for the projection to state the record's status, then retry.",
        )
    unknown = ""
    if status is None:
        unknown = "states no status · it may not confirm"
    elif (mutation.entity, status) in AMBIGUOUS_STATES:
        unknown = f"is {status}, whose outcome is not yet observed · it may not confirm"
    request = None
    if refusal is None:
        request = LifecycleRequest(
            target=row.key, method=mutation.method, revision=revision, operation_id=operation_id
        )
    return Item(
        key=row.key,
        title=row.title,
        revision=revision,
        status=status,
        effects=stated.effects,
        not_effects=stated.not_effects,
        refusal=refusal,
        unknown=unknown,
        request=request,
        stale_token=str(revision),
        clock_fields=stated.clock_fields,
    )


def _authority(klass: str, principal: str) -> str:
    who = f"acting as {principal}" if principal else "acting as the daemon"
    return f"{klass} · {who} · the daemon checks the class at commit"


def lifecycle_card(
    mutation: CanonicalMutation,
    targets: Sequence[ProjectionRow],
    *,
    principal: str,
    now: float,
    reuse: Mapping[str, str] | None = None,
) -> Card:
    """Return the card previewing ``mutation`` on every target, one row each.

    Args:
        mutation: The lifecycle verb.
        targets: The rows it acts on, in selection order.
        principal: Who the console acts as; empty when it acts as nobody.
        now: The console clock.
        reuse: Operation ids to reuse by target key, for a repeat at an unchanged
            revision; every other target gets a fresh id.

    Raises:
        ValueError: ``targets`` is empty.
    """
    if not targets:
        raise ValueError("a card needs at least one target")
    ids = reuse or {}
    items = tuple(_item(mutation, row, ids.get(row.key) or mint_lifecycle_id()) for row in targets)
    revision = items[0].revision or 1
    stale = (
        if_stale(revision)
        if len(items) == 1
        else "if any bound revision moves before you confirm, this card reloads at the "
        "current revisions and every authorization is withdrawn, never re-targeted"
    )
    return Card(
        kind="lifecycle",
        origin=mutation.method,
        action=mutation.action,
        noun=mutation.entity.value,
        items=items,
        if_stale=stale,
        authority=_authority(mutation.authority, principal),
        issuer=principal,
        opened_at=now,
    )


def setting_card(request: SettingRequest, *, effect: str, token: str, now: float) -> Card:
    """Return the card previewing one settings write or unset.

    Args:
        request: The edit, carrying the key, the layer and the typed value.
        effect: What the effective value becomes, as the settings model words it.
        token: The effective value the card was built against.
        now: The console clock.
    """
    verb = "unset" if request.unset else "set"
    change = (
        f"{request.target} is removed from the {request.layer} layer file"
        if request.unset
        else f"{request.target} = {request.value!r} is written to the {request.layer} layer file"
    )
    item = Item(
        key=request.target,
        title=f"{request.layer} layer · no revision · the file is written under the daemon's lock",
        revision=None,
        status=None,
        effects=(change, effect),
        not_effects=(
            f"no layer other than {request.layer} is written",
            "no run, task or batch is moved by it",
        ),
        refusal=None,
        unknown="",
        request=request,
        stale_token=token,
    )
    return Card(
        kind="setting",
        origin=request.target,
        action=f"{verb} {request.target}",
        noun="setting",
        items=(item,),
        if_stale=(
            "if the settings are re-read with another value before you confirm, this card "
            "reloads against it and the edit is withdrawn"
        ),
        authority="config write · the daemon writes the layer file under its own lock",
        opened_at=now,
    )


def answer_card(row: ProjectionRow, verb_key: str, *, principal: str, now: float) -> Card:
    """Return the card previewing an answer to one held pending action.

    Args:
        row: The pending action as the Attention projection holds it.
        verb_key: The attention verb letter the operator chose.
        principal: Who the answer is sealed in the name of.
        now: The console clock.
    """
    name = VERB[verb_key].name
    option = ANSWER_OPTIONS.get(name)
    revision = int(row.revision)
    refusal = None
    request: VerbRequest | None = None
    if option is None:
        why = binding_refusal(ATTENTION_ROUTE, name)
        refusal = Refusal(code="unbound_verb", reason=why, remediation="Answer or deny instead.")
    else:
        request = AnswerRequest(target=row.key, option_id=option)
    item = Item(
        key=row.key,
        title=row.title,
        revision=revision,
        status=status_of(row),
        effects=(
            f"{row.key} is sealed {option!r} in your name, under your evidence receipt",
            "the question closes for every principal; a later answer is superseded",
        ),
        not_effects=_ANSWER_NOT,
        refusal=refusal,
        unknown="",
        request=request,
        stale_token=str(revision),
    )
    return Card(
        kind="answer",
        origin=verb_key,
        action=name,
        noun="pending action",
        items=(item,),
        if_stale=if_stale(revision),
        authority=_authority("answer", principal),
        issuer=principal,
        opened_at=now,
    )


def control_card(
    row: ProjectionRow, target: Mapping[str, str], *, principal: str, now: float
) -> Card:
    """Return the card previewing one Run control on a held Run.

    Args:
        row: The Run as the link holds it.
        target: The control the frame asked for: its verb, effects and non-effects.
        principal: Who the request is recorded in the name of.
        now: The console clock.
    """
    verb = target["verb"]
    revision = int(row.revision)
    item = Item(
        key=row.key,
        title=row.title,
        revision=revision,
        status=status_of(row),
        effects=(f"a {verb} request is recorded against {row.key}", target.get("effects", "")),
        not_effects=(_CONTROL_NOT, target.get("not", "")),
        refusal=None,
        unknown="the run answers on its own time · the outcome stays unknown until it does",
        request=ControlRequest(target=row.key, control=RUN_CONTROLS[verb]),
        stale_token=str(revision),
    )
    return Card(
        kind="control",
        origin=verb,
        action=verb,
        noun="run",
        items=(item,),
        if_stale=if_stale(revision),
        authority=_authority("control", principal),
        issuer=principal,
        opened_at=now,
    )


def setting_token(leaf: SettingsLeaf) -> str:
    """Return what a settings card is bound to: every layer's statement and the winner."""
    stack = tuple((str(entry.layer), repr(entry.value)) for entry in leaf.stack)
    return repr((str(leaf.source_layer), repr(leaf.effective.value), stack))


def stamp(card: Card, now: float) -> str:
    """Return a stamp on the card's own clock: seconds since it was built."""
    return f"+{max(now - card.opened_at, 0.0):.1f}s"


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
    first = card.items[0].refusal
    return Availability(False, first.reason if first is not None else "refused")


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
        leave_overlay(s)
        return select_key(ctx, k)
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
    return str(operation_id) if int(revision) == int(row.revision) else None


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


def open_setting(ctx: Ctx, request: SettingRequest, *, effect: str, token: str) -> None:
    """Open the card previewing one settings edit, or say why no request can leave."""
    key = "x" if request.unset else "Enter"
    decided = _gate(ctx, f"set {request.target}", needs_principal=False)
    if decided.kind is GateKind.TRANSPORT:
        ctx.log(key, f"no daemon link · nothing was written · {decided.reason}")
        return
    if decided.kind is GateKind.REFUSED:
        ctx.log(key, f"refused: {decided.reason}")
        return
    ctx.s.mutation = setting_card(request, effect=effect, token=token, now=ctx.clock.now())
    open_overlay(ctx.s, CARD, subject=request.target)
    ctx.log(key, f"{request.target} · {effect} → consequence preview")


def adopt(ctx: Ctx) -> None:
    """Build the card a consequence overlay opened elsewhere needs, from what the link holds.

    An attention verb and a Run control open the consequence overlay by name; on a linked
    console the card they preview is built here, from the held pending action or the
    held Run, so the card states the revision the write will be addressed at. Nothing is
    built for a console holding the prototype registers, whose card is the golden one.
    """
    s = ctx.s
    if s.overlay != CARD:
        s.mutation = None
        return
    if s.mutation is not None or ctx.fixture.prototype:
        return
    now = ctx.clock.now()
    target = s.c_target
    if target is None and s.route == ATTENTION_ROUTE and ctx.attention is not None:
        row = selected_open_row(s, ctx.attention)
        if row is not None:
            s.mutation = answer_card(row, s.verb or "a", principal=principal_of(ctx), now=now)
        return
    if target is not None and target.get("kind") in RUN_KINDS and target["verb"] in RUN_CONTROLS:
        row = _row(ctx.rows, target["id"])
        if row is not None:
            s.mutation = control_card(row, target, principal=principal_of(ctx), now=now)


def _current_token(ctx: Ctx, card: Card, item: Item) -> str | None:
    """Return what the target reads as now, in the card's stale-token terms."""
    if card.kind == "setting":
        settings = ctx.settings
        return setting_token(settings.leaf(item.key)) if settings is not None else None
    if card.kind == "answer" and ctx.attention is not None:
        row = _row(ctx.attention.rows, item.key)
    else:
        row = _row(ctx.rows, item.key)
    return str(row.revision) if row is not None else None


def _reload(ctx: Ctx, card: Card, moved: Sequence[tuple[Item, str | None]]) -> None:
    """Rebuild the card at the targets' current revisions; the old authorization is gone."""
    s = ctx.s
    said = ", ".join(f"{item.key} {item.stale_token} → {now or 'gone'}" for item, now in moved)
    note = f"reloaded · {said} · the authorization was withdrawn"
    rebuilt: Card | None = None
    now = ctx.clock.now()
    if card.kind == "lifecycle":
        rows = tuple(row for item in card.items if (row := _row(ctx.rows, item.key)) is not None)
        if rows:
            rebuilt = lifecycle_card(
                MUTATIONS_BY_METHOD[card.origin], rows, principal=principal_of(ctx), now=now
            )
    elif card.kind == "answer" and ctx.attention is not None:
        row = _row(ctx.attention.rows, card.items[0].key)
        if row is not None:
            rebuilt = answer_card(row, card.origin, principal=principal_of(ctx), now=now)
    elif card.kind == "control":
        row = _row(ctx.rows, card.items[0].key)
        target = s.c_target
        if row is not None and target is not None:
            rebuilt = control_card(row, target, principal=principal_of(ctx), now=now)
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
        first = card.items[0].refusal
        ctx.log("Enter", f"{card.action} refused · {first.reason if first else 'refused'}")
        return
    now = ctx.clock.now()
    results: list[Result] = []
    for item in card.items:
        if item.request is None:
            why = item.refusal
            results.append(
                Result(
                    key=item.key,
                    disposition=ControlDisposition.IDLE,
                    detail=f"{why.code} · {why.reason}" if why else "refused on the card",
                )
            )
            continue
        results.append(_send(ctx, card, item, now))
    s.mutation = replace(card, results=tuple(results))
    sent = sum(1 for row in results if row.requested != NO_STAMP)
    ctx.log("Enter", f"{card.action} · {sent} of {len(results)} sent · waiting for the daemon")


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
    """Run a selection key from the route's key path, or record it unclaimed."""
    if pane or not select_key(ctx, k):
        ctx.noop(k)


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


# ---------- the daemon's answer ----------

#: The outcomes a reconcile may still answer: nothing was heard, or asking found nothing.
RECONCILABLE: Final = frozenset({ControlDisposition.UNKNOWN, ControlDisposition.RECOVERY})
_ACCEPTED: Final = frozenset({ControlDisposition.ACCEPTED, ControlDisposition.CONFIRMED})


def settle(session: Session, result: OperationResult, now: float) -> None:
    """Fill the open card's row for the target ``result`` answers, from the daemon's answer.

    The row takes the result's own disposition. Its stamps follow it: ``accepted`` once the
    daemon took the request, ``confirmed`` once the effect is recorded, and the answer
    stamp whenever an answer arrived at all, so an unknown row carries none of them.
    """
    card = session.mutation
    if not isinstance(card, Card) or not card.results:
        return
    index = _answered(card, result)
    if index is None:
        return
    row = card.results[index]
    at = stamp(card, now)
    outcome = result.disposition
    settled = replace(
        row,
        disposition=outcome,
        detail=result.detail,
        accepted=at if outcome in _ACCEPTED else row.accepted,
        confirmed=at if outcome is ControlDisposition.CONFIRMED else row.confirmed,
        answered=row.answered if outcome in RECONCILABLE else at,
        revision=result.revision,
        answered_by=result.answered_by,
    )
    rows = list(card.results)
    rows[index] = settled
    session.mutation = replace(card, results=tuple(rows))


def _answered(card: Card, result: OperationResult) -> int | None:
    """Return the row ``result`` answers: by operation id, else the target's open row."""
    for item in card.items:
        request = item.request
        if isinstance(request, LifecycleRequest) and request.operation_id == result.operation_id:
            return next(i for i, row in enumerate(card.results) if row.key == item.key)
    return next(
        (
            i
            for i, row in enumerate(card.results)
            if row.key == result.target
            and row.disposition in {ControlDisposition.REQUESTING, *RECONCILABLE}
        ),
        None,
    )


__all__ = [
    "CARD",
    "NATIVE_KEYS",
    "NO_LINK_REASON",
    "RECONCILABLE",
    "Card",
    "Gate",
    "GateKind",
    "Item",
    "Result",
    "adopt",
    "answer_card",
    "card_key",
    "confirm",
    "control_card",
    "gate",
    "lifecycle_card",
    "lifecycle_verbs",
    "menu_key",
    "select_key",
    "setting_card",
    "setting_token",
    "settle",
    "verb_check",
]
