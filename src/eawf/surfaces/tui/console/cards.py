"""The consequence card: what one confirmed write will do, target by target, and its answers.

Every canonical mutation a linked console can send is previewed on one card: a lifecycle
move on a Track, Milestone, Batch, Task or Run, a settings write or unset, an answer to a
pending action or permission, a budget notice disposition, a Run control, and a dispatch
queue request. Nothing is sent while the card only previews. The card is built from the
row the link holds -- its status and its revision -- and from the transition table, so
what it says the move will change is what the daemon will judge; a move the request itself
cannot satisfy is refused on the card with the denial's own code and remediation, and
nothing is sent for it. Each target carries the token its card compares at confirmation
to tell a target that moved underneath it.

Once confirmed, each target keeps its own result row: requested, accepted and confirmed
stamps and the outcome it reached, filled from the daemon's answer and never from the request. A
target the daemon never answered reads ``unknown`` until reconcile asks again under the
same operation id; it is never counted as a success or a failure.

The single write gate decides what a verb may do before it is chosen. With no request
able to leave the console -- no link, or a link that lost its transport -- the lifecycle
verbs are removed from the menu, because there is nothing to refuse. With a link that
refuses writes for a reason the operator can act on, the verbs stay listed with that
reason. The two never render alike.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal

from eawf.kernel.delivery.bulk import BulkVerb
from eawf.kernel.projection.attention import CONSOLE_PRINCIPAL_CLASS
from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.settings import SettingsLeaf
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.runtime.dispatch_queue import DispatchQueueView
from eawf.kernel.state.epoch2.consequence import (
    CanonicalMutation,
    Refusal,
    consequence,
    if_stale,
)
from eawf.kernel.state.epoch2.transitions import (
    AMBIGUOUS_STATES,
    DenialCode,
)
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.surfaces.tui.console.attention import VERB
from eawf.surfaces.tui.console.attention_verbs import action_request
from eawf.surfaces.tui.console.bulk import BULK_METHODS
from eawf.surfaces.tui.console.eligibility import eligible_pane, principals_of
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.live_reads import held_queue
from eawf.surfaces.tui.console.notices import NOTICE_VERBS, short_key
from eawf.surfaces.tui.console.operations import (
    DISPATCH_VERBS,
    PERMISSION_VERBS,
    RUN_CONTROLS,
    SNOOZE_FOR,
    ControlRequest,
    DispatchRequest,
    LifecycleRequest,
    NoticeRequest,
    OperationResult,
    PermissionDecision,
    SettingRequest,
    VerbRequest,
    mint_lifecycle_id,
)
from eawf.surfaces.tui.console.reads import mut_reason, transport_lost, write_refusal
from eawf.surfaces.tui.console.session import Session

#: The overlay the card draws in.
CARD = "consequence"
#: Why no request can be issued on a console started without a daemon link.
NO_LINK_REASON: Final = "the console holds no daemon link · no request can be issued"
#: A stamp that has not happened.
NO_STAMP: Final = "—"

_ANSWER_NOT: Final = (
    "no lifecycle moves: no run is started, stopped or re-owned",
    "it does not accept the milestone the question is about",
)
_STAYS_OPEN: Final = "it stays open for the rest"
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


Kind = Literal["lifecycle", "setting", "answer", "control", "notice", "dispatch"]


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
        changes: The per-member difference a whole-value write makes, for a settings
            list or mapping; empty for every other write.
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
    changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a target that is neither sent nor refused, or both.

        Raises:
            ValueError: the target carries a request and a refusal, or neither.
        """
        if (self.request is None) == (self.refusal is None):
            raise ValueError(f"{self.key} is sent exactly when it is not refused")

    @property
    def why(self) -> str:
        """Return why the target is refused on the card; empty for a target that is sent."""
        return self.refusal.reason if self.refusal is not None else ""


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
        eligible: The eligible pane of an action several principals may answer; empty
            where one principal alone may.
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
    eligible: tuple[str, ...] = ()

    @property
    def bulk(self) -> bool:
        """Return whether the card previews more than one target."""
        return len(self.items) > 1

    @property
    def bulk_verb(self) -> BulkVerb | None:
        """Return the bulk verb a confirmed selection is sent as, or ``None``."""
        return BULK_METHODS.get(self.origin) if self.kind == "lifecycle" and self.bulk else None

    @property
    def sendable(self) -> tuple[Item, ...]:
        """Return the targets confirmation sends: the ones not refused on the card."""
        return tuple(item for item in self.items if item.request is not None)


def status_of(row: ProjectionRow) -> str | None:
    """Return the row's status when the projection states it, else ``None``."""
    field = row.status
    return field.value if field.state is TruthState.KNOWN else None


def _item(
    mutation: CanonicalMutation, row: ProjectionRow, operation_id: str, *, in_bulk: bool
) -> Item:
    """Return one lifecycle target, previewed at the row's own status and revision.

    A target sent in a bulk operation is not refused for lacking a recorded reason: the
    bulk verb records its own cause on every item.
    """
    status = status_of(row)
    revision = int(row.revision)
    stated = consequence(mutation, key=row.key, revision=revision, status=status)
    refusal = stated.refusal
    if in_bulk and refusal is not None:
        refusal = None if refusal.code == DenialCode.TRANSITION_REASON_MISSING.value else refusal
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
    in_bulk = len(targets) > 1 and mutation.method in BULK_METHODS
    items = tuple(
        _item(mutation, row, ids.get(row.key) or mint_lifecycle_id(), in_bulk=in_bulk)
        for row in targets
    )
    revision = int(targets[0].revision)
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


def setting_card(
    request: SettingRequest,
    *,
    effect: str,
    token: str,
    now: float,
    changes: tuple[str, ...] = (),
) -> Card:
    """Return the card previewing one settings write or unset.

    Args:
        request: The edit, carrying the key, the layer and the typed value.
        effect: What the effective value becomes, as the settings model words it.
        token: The effective value the card was built against.
        now: The console clock.
        changes: The per-member difference of a whole list or mapping written at once;
            the card then states the difference rather than the whole value.
    """
    verb = "unset" if request.unset else "set"
    if request.unset:
        change = f"{request.target} is removed from the {request.layer} layer file"
    elif changes:
        change = f"{request.target} is written whole to the {request.layer} layer file"
    else:
        change = (
            f"{request.target} = {request.value!r} is written to the {request.layer} layer file"
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
        changes=changes,
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


def _permission_card(row: ProjectionRow, verb_key: str, *, principal: str, now: float) -> Card:
    """Return the card previewing an operator's approval or denial of one provider permission.

    The verb is refused on the card when it decides nothing or when the operator class is
    not among the classes the permission admits for it; the daemon refuses both again at
    commit. There is no hold to offer: the provider owns the deadline.
    """
    name = VERB[verb_key].name
    decided = PERMISSION_VERBS.get(name)
    revision = int(row.revision)
    admitted = [c.strip() for c in row.facts.get(decided or "", "").split(",") if c.strip()]
    refusal = None
    request: VerbRequest | None = None
    if decided is None:
        refusal = Refusal(
            code="unbound_verb",
            reason=f"a provider permission is approved or denied, never {name.rstrip('e')}ed",
            remediation="Approve or deny it before the provider's deadline.",
        )
    elif CONSOLE_PRINCIPAL_CLASS not in admitted:
        refusal = Refusal(
            code="authority_denied",
            reason=f"{decided} is for {', '.join(admitted) or 'no class'} only",
            remediation="Ask a principal of that class to decide it.",
        )
    else:
        request = PermissionDecision(target=row.key, verb=decided)
    deadline = row.facts.get("deadline_at", "an unstated time")
    item = Item(
        key=row.key,
        title=row.title,
        revision=revision,
        status=status_of(row),
        effects=(
            f"{row.key} is recorded {decided or name} in your name, as the operator",
            f"the provider's own deadline still runs out at {deadline}",
        ),
        not_effects=(
            "no pending action on the same run is answered",
            "no hold is placed: the provider owns the deadline and it expires",
        ),
        refusal=refusal,
        unknown="",
        request=request,
        stale_token=str(revision),
    )
    return Card(
        kind="answer",
        origin=verb_key,
        action=name,
        noun="provider permission",
        items=(item,),
        if_stale=if_stale(revision),
        authority=_authority(CONSOLE_PRINCIPAL_CLASS, principal),
        issuer=principal,
        opened_at=now,
    )


def answer_card(
    row: ProjectionRow,
    verb_key: str,
    *,
    principal: str,
    now: float,
    wall: datetime,
    rows: Sequence[ProjectionRow],
) -> Card:
    """Return the card previewing one attention verb on a held pending action or permission.

    Args:
        row: The pending action or permission as the Attention projection holds it.
        verb_key: The attention verb letter the operator chose.
        principal: Who the answer is sealed in the name of.
        now: The console clock.
        wall: The wall-clock instant a snooze is measured from.
        rows: The held Attention register, which names the principals who may answer.
    """
    if row.collection is Epoch2Collection.PERMISSION:
        return _permission_card(row, verb_key, principal=principal, now=now)
    name = VERB[verb_key].name
    revision = int(row.revision)
    known = principals_of(rows, principal)
    request, refusal, effects = action_request(
        row, name, principal=principal, known=known, wall=wall
    )
    item = Item(
        key=row.key,
        title=row.title,
        revision=revision,
        status=status_of(row),
        effects=effects or (f"nothing is sent for {row.key}",),
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
        eligible=eligible_pane(row, known, principal),
    )


def notice_card(
    notice: BudgetThresholdNotice, verb_key: str, *, principal: str, now: float, wall: datetime
) -> Card:
    """Return the card previewing a disposition of one held budget notice.

    Args:
        notice: The notice as the operator's inbox holds it.
        verb_key: ``z`` to snooze it, ``n`` to acknowledge it or ``v`` to resolve it.
        principal: Who the disposition is recorded as.
        now: The console clock.
        wall: The wall-clock instant a snooze is measured from.
    """
    disposition = NOTICE_VERBS[verb_key]
    until = wall + SNOOZE_FOR if disposition == "snooze" else None
    named = f"notice {short_key(notice)}"
    effects: tuple[str, ...]
    if until is not None:
        effects = (f"{named} leaves your inbox until {until:%H:%M} UTC", _STAYS_OPEN)
    elif disposition == "acknowledge":
        effects = (f"{named} is acknowledged by you alone, and not resolved", _STAYS_OPEN)
    else:
        effects = (f"{named} closes for its whole audience, resolved in your name",)
    item = Item(
        key=notice.notice_key,
        title=f"{notice.scope_id} {notice.axis} over budget",
        revision=notice.revision,
        status=notice.status,
        effects=effects,
        not_effects=("no run is stopped, extended or restarted by it",),
        refusal=None,
        unknown="",
        request=NoticeRequest(
            target=notice.notice_key,
            disposition=disposition,
            revision=notice.revision,
            snooze_until=until,
        ),
        stale_token=str(notice.revision),
    )
    return Card(
        kind="notice",
        origin=verb_key,
        action=disposition,
        noun="budget notice",
        items=(item,),
        if_stale=if_stale(notice.revision),
        authority=_authority("notice disposition", principal),
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


def dispatch_card(
    live: Mapping[str, object], target: Mapping[str, str], *, principal: str, now: float
) -> Card:
    """Return the card previewing one pause, drain or resume request of the dispatch queue.

    Args:
        live: The frame's live answers, the dispatch-queue read among them once it arrived.
        target: The request the frame asked for: its verb, effects and non-effects.
        principal: Who the request is recorded in the name of.
        now: The console clock.
    """
    queue = held_queue(live)
    verb, held = target["verb"], queue.control.holding if queue is not None else None
    request = DispatchRequest(verb=DISPATCH_VERBS[verb])
    item = Item(
        key=request.target,
        title="the daemon owns scheduling; this console only asks",
        revision=None,
        status=f"held by {held.value}" if held is not None else "dispatching",
        effects=(f"a {verb} is recorded for the dispatch scheduler", target.get("effects", "")),
        not_effects=("no claimed run is stopped or cancelled", target.get("not", "")),
        refusal=None,
        unknown="the daemon refuses a drain while a release is publishing",
        request=request,
        stale_token=dispatch_token(queue),
    )
    return Card(
        kind="dispatch",
        origin=verb,
        action=verb,
        noun="dispatch queue",
        items=(item,),
        if_stale=(
            "if the dispatch control moves before you confirm, this card reloads against it "
            "and the request is withdrawn"
        ),
        authority=_authority("control", principal),
        issuer=principal,
        opened_at=now,
    )


def dispatch_token(queue: DispatchQueueView | None) -> str:
    """Return what a dispatch card is bound to: the control standing and its newest request.

    The daemon records every request, confirmed or refused, as the newest one, so a control
    that moved under an open card never reads as the control the card was opened on.
    """
    if queue is None:
        return "unread"
    control = queue.control
    held = control.holding.value if control.holding is not None else "dispatching"
    last = control.last_request
    return f"{held} after {last.request_ref}" if last is not None else f"{held} before any request"


def setting_token(leaf: SettingsLeaf) -> str:
    """Return what a settings card is bound to: every layer's statement and the winner."""
    stack = tuple((str(entry.layer), repr(entry.value)) for entry in leaf.stack)
    return repr((str(leaf.source_layer), repr(leaf.effective.value), stack))


def stamp(card: Card, now: float) -> str:
    """Return a stamp on the card's own clock: seconds since it was built."""
    return f"+{max(now - card.opened_at, 0.0):.1f}s"


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
    """Return the row ``result`` answers: by operation id, else the target's open row.

    A bulk operation answers every target under one id, so the target decides the row.
    """
    for item in card.items:
        request = item.request
        if (
            isinstance(request, LifecycleRequest)
            and request.operation_id == result.operation_id
            and item.key == result.target
        ):
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
    "NO_LINK_REASON",
    "NO_STAMP",
    "RECONCILABLE",
    "Card",
    "Gate",
    "GateKind",
    "Item",
    "Kind",
    "Result",
    "answer_card",
    "control_card",
    "dispatch_card",
    "dispatch_token",
    "gate",
    "lifecycle_card",
    "notice_card",
    "setting_card",
    "setting_token",
    "settle",
    "stamp",
    "status_of",
]
