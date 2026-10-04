"""The console's writes: which verbs reach a daemon mutator, and the ledger of what was sent.

A console verb never changes what the console holds. It is sent to the daemon as one
operation named by an operation id, and the only thing that moves the frame afterwards is
the daemon's own answer and the patch its commit pushes. The id is what the daemon files
the write under, so sending the same operation twice is one write: that is what lets a
reconnect reconcile an operation whose answer was lost by simply asking again under the
same id, instead of guessing whether it landed.

Seven daemon mutators are bound. An answer to a pending action goes to the approval seal,
which reports a later conflicting answer as superseded rather than refusing it; an answer
to a provider permission goes to the permission's own decide verb, as the operator, and
never to the seal, because the two records resolve apart; a Run
control goes to the control-request verb, which records that a principal asked and moves
the Run not at all; a settings edit goes to the layered-config verbs, which write one
layer file under the daemon's lock; and a lifecycle move goes to the per-entity verb that
names it, addressed at the revision its consequence card was built at and filed under the
operation id the card minted, so confirming the same card twice is one write; and a budget
notice's snooze or resolve goes to the notice ledger's disposition verb, at the revision the
operator was shown; and a pending action's snooze or assignment goes to the pending-action
disposition verbs, at the revision the operator was shown; and a Campaign's drop goes to
the Campaign close verb, at the revision the operator was shown; and a proposed Milestone
date goes to the Milestone's target-date verb, at the revision the operator was shown,
filed under the operation id as its idempotency key. Every other
writing verb stays listed and refused with its reason, because a verb that looked like it
worked while the daemon never heard of it is the one thing a console must not draw.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal

from eawf.kernel.projection.attention import CONSOLE_PRINCIPAL_CLASS
from eawf.kernel.runtime.control import ControlDisposition
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.consequence import MUTATIONS_BY_METHOD
from eawf.workflow.delivery.acceptance_approval import ACCEPTANCE_OPTIONS

logger = logging.getLogger(__name__)

#: The daemon verb that seals an answer to a pending action. Spelled here rather than
#: imported, because importing the daemon's method module registers its handlers in the
#: console's process; a contract test pins the two spellings together.
SEAL_METHOD: Final = "runtime.delivery.seal_acceptance_approval"

#: The daemon verbs that record one principal's own snooze of a pending action, and that
#: address a pending action to one principal or to everyone.
ACTION_SNOOZE_METHOD: Final = "runtime.pending_action.snooze"
ACTION_ASSIGN_METHOD: Final = "runtime.pending_action.assign"

#: The daemon verb that records a principal's request for a Run control.
CONTROL_METHOD: Final = "runtime.run.control.request"

#: The daemon verb that records a pause, drain or resume of dispatch.
DISPATCH_CONTROL_METHOD: Final = "runtime.dispatch.control.request"

#: The key a dispatch request is filed under: the queue is the tree's one scheduler.
DISPATCH_QUEUE_TARGET: Final = "dispatch queue"

#: The daemon verb that closes an active Campaign as converged or cancelled.
CAMPAIGN_CLOSE_METHOD: Final = "runtime.campaign.close"

#: The daemon verb that sets the calendar day a Milestone is aimed at; no status moves.
MILESTONE_TARGET_METHOD: Final = "domain.milestone.set_target"

#: The daemon verb that approves or denies a provider permission.
PERMISSION_DECIDE_METHOD: Final = "runtime.permission.decide"

#: The daemon verb that records an operator's answer to a question, by option or reply.
QUESTION_ANSWER_METHOD: Final = "runtime.question.answer"

#: The wire code a write refused for naming a revision the record has moved past carries.
STALE_REVISION_CODE: Final = "revision_conflict"

#: The daemon verbs a settings edit writes and removes one layer's value through. The
#: console never writes a layer file itself: the daemon holds the file lock and checks the
#: key against the leaf catalog before it writes.
SETTING_SET_METHOD: Final = "config.set_layer_value"
SETTING_UNSET_METHOD: Final = "config.unset_layer_value"
#: The daemon verb that writes several keys of one layer at once, for keys only valid
#: together; the daemon checks their section once, after all of them.
SETTING_SET_MANY_METHOD: Final = "config.set_layer_values"

#: The daemon verbs that list one principal's budget notices and record what they did to
#: one. A notice is not a pending action: it blocks nothing and is answered by nobody.
NOTICE_LIST_METHOD: Final = "budget_notice.list"
NOTICE_DISPOSE_METHOD: Final = "budget_notice.dispose"

#: How long a console snooze keeps a notice out of the principal's inbox. The ledger takes
#: a deadline rather than a duration, and the console offers one snooze length.
SNOOZE_FOR: Final = timedelta(hours=1)

#: The layers a settings edit may target: the five file layers the lens cycles.
SETTING_LAYERS: Final = frozenset({"global", "workspace", "repo", "branch", "local"})

#: The route whose verbs answer pending actions.
ATTENTION_ROUTE: Final = "attention"

#: The target kinds a Run control addresses: the Run's own route, the Run register, and
#: the pause card's.
RUN_KINDS: Final = frozenset({"run.detail", "activity", "run"})

#: The answer an attention verb gives, by verb name. The daemon seals only the
#: acceptance approval, so its option ids are the answers a console can give.
ANSWER_OPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {"answer": "approve", "deny": "decline"}
)

#: The permission verb each attention verb decides a provider permission with. There is
#: no hold among them: the provider owns the deadline, so there is nothing to hold.
PERMISSION_VERBS: Final[Mapping[str, str]] = MappingProxyType({"answer": "approve", "deny": "deny"})

#: The question overlay's numbered answers, in the order the approval offers them.
QUESTION_OPTIONS: Final[tuple[str, ...]] = tuple(o.option_id for o in ACCEPTANCE_OPTIONS)

#: The Run control each bound run verb requests, by verb name.
RUN_CONTROLS: Final[Mapping[str, ControlKind]] = MappingProxyType(
    {
        "interrupt": ControlKind.INTERRUPT,
        "cancel": ControlKind.CANCEL,
        "reconcile": ControlKind.RECONCILE,
        "resume": ControlKind.RESUME,
        "cancel selected": ControlKind.CANCEL,
        "reconcile selected": ControlKind.RECONCILE,
    }
)

#: The attention verbs a pending action is disposed of through without being answered.
ACTION_VERBS: Final[Mapping[str, str]] = MappingProxyType(
    {"snooze": ACTION_SNOOZE_METHOD, "assign": ACTION_ASSIGN_METHOD}
)

#: The target kinds a dispatch request addresses: the queue's route, its own card, and a
#: Track, whose pause asks the tree's one queue to pause.
DISPATCH_KINDS: Final = frozenset({"unattended", "dispatch queue", "track"})

#: The dispatch control each queue verb requests, by verb name.
DISPATCH_VERBS: Final[Mapping[str, Literal["pause", "drain", "resume"]]] = MappingProxyType(
    {
        "request pause": "pause",
        "request drain": "drain",
        "request resume": "resume",
        "pause dispatch": "pause",
    }
)

#: The route whose lens verbs write one layer's value through the layered-config verbs.
SETTINGS_ROUTE: Final = "settings"
#: The settings verbs the lens carries to a layer write or unset.
SETTING_VERBS: Final = frozenset({"edit", "unset"})

#: The route whose Campaign the drop verb closes, and the verb.
CAMPAIGN_ROUTE: Final = "campaign"
DROP_CAMPAIGN_VERB: Final = "drop campaign"
#: Why a Campaign the console drops was closed, as the Campaign's stop records it.
DROP_REASON: Final = "dropped by the operator from the console"

#: The route whose undated Milestones the operator proposes a target date for, and the verb.
TIMELINE_ROUTE: Final = "timeline"
PROPOSE_DATE_VERB: Final = "propose date"

#: The Attention verb that closes every marked question as no longer needing an answer:
#: each is answered through the question answer verb with one fixed reply, one write per
#: question, so a backlog of stale questions is cleared in one decision.
DISMISS_VERB: Final = "dismiss selected"
#: The reply a dismissed question is answered with, so its record says why it closed.
DISMISS_REPLY: Final = "dismissed from the console: it no longer needs an answer"

#: Why a writing verb with no daemon mutator is refused; the reason the menu shows.
UNBOUND_REASON: Final = "no daemon verb carries this yet"
_UNBOUND_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "resolve": "a pending action closes only by its answer · "
        "a answer or x deny seals it for every principal",
        "acknowledge": "only a notice is acknowledged · a pending action is answered",
    }
)

#: Why every bound verb is refused on a console linked to a daemon but acting as nobody. The
#: menu shows it before a verb is chosen, so a write that could never be attributed is not
#: offered as though it could.
NO_PRINCIPAL_REASON: Final = "no operator principal to act as · relaunch with --actor"

#: How many random bytes an operation id carries.
_ID_BYTES: Final = 8


#: A chrome menu verb that is a per-entity lifecycle verb, by the daemon verb that carries it.
SAME_VERB: Final[Mapping[str, str]] = MappingProxyType(
    {
        "retire track": "domain.track.retire",
        "accept": "domain.milestone.accept",
        "authorize merge": "domain.batch.merge",
        "promote": "domain.task.promote",
    }
)


def linked_refusal(kind: str, verb: str) -> str:
    """Return why a chrome verb is refused on a console reading a live tree, or nothing.

    The chrome's own availability and reasons describe the prototype's records, never the
    tree on screen. A lifecycle verb is offered by the record it moves, so where no such
    record is selected the chrome's copy of it is refused naming what it acts on; any other
    verb is refused unless a daemon verb carries it.

    Args:
        kind: The route the verb is listed on.
        verb: The verb's name, as the menu lists it.

    Returns:
        An empty string when a daemon verb carries the verb; otherwise the reason.
    """
    method = SAME_VERB.get(verb)
    if method is not None:
        return f"acts on a {MUTATIONS_BY_METHOD[method].entity.value} · open one to offer it"
    return binding_refusal(kind, verb)


def binding_refusal(kind: str, verb: str) -> str:
    """Return why ``verb`` on a ``kind`` target reaches no daemon mutator.

    Args:
        kind: The route or target kind the verb acts on (``attention``, ``run.detail``).
        verb: The verb's name, as the menu lists it.

    Returns:
        An empty string when a daemon mutator carries the verb; otherwise the reason the
        verb is refused.
    """
    if kind == ATTENTION_ROUTE and (
        verb in ANSWER_OPTIONS or verb in ACTION_VERBS or verb == DISMISS_VERB
    ):
        return ""
    if kind in RUN_KINDS and verb in RUN_CONTROLS:
        return ""
    if kind in DISPATCH_KINDS and verb in DISPATCH_VERBS:
        return ""
    if (kind, verb) in ((CAMPAIGN_ROUTE, DROP_CAMPAIGN_VERB), (TIMELINE_ROUTE, PROPOSE_DATE_VERB)):
        return ""
    if kind == SETTINGS_ROUTE and verb in SETTING_VERBS:
        return ""
    return _UNBOUND_REASONS.get(verb, UNBOUND_REASON)


@dataclass(frozen=True, slots=True, kw_only=True)
class AnswerRequest:
    """An operator's answer to one pending action, before it is addressed.

    Attributes:
        target: The pending action's public key.
        option_id: The option the operator chose.
    """

    target: str
    option_id: str

    def __post_init__(self) -> None:
        """Refuse an option the approval does not offer.

        Raises:
            ValueError: ``option_id`` is not one of the approval's options.
        """
        if self.option_id not in QUESTION_OPTIONS:
            raise ValueError(
                f"option {self.option_id!r} is not one of {', '.join(QUESTION_OPTIONS)}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class QuestionAnswer:
    """An operator's answer to one question, before it is addressed.

    Attributes:
        target: The question's public key.
        option_key: The option chosen, for an answer by option.
        reply: The operator's own words, for an answer by reply.
    """

    target: str
    option_key: str | None = None
    reply: str | None = None

    def __post_init__(self) -> None:
        """Refuse an answer that is both or neither of an option and a reply.

        Raises:
            ValueError: Both or neither is given.
        """
        if (self.option_key is None) == (self.reply is None):
            raise ValueError("an answer to a question is exactly one of an option or a reply")


@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionDecision:
    """An operator's approval or denial of one provider permission, before it is addressed.

    Attributes:
        target: The permission's public key.
        verb: ``approve`` or ``deny``.
    """

    target: str
    verb: str

    def __post_init__(self) -> None:
        """Refuse a verb that does not decide a permission.

        Raises:
            ValueError: ``verb`` is neither approve nor deny.
        """
        if self.verb not in PERMISSION_VERBS.values():
            raise ValueError(f"verb {self.verb!r} does not decide a provider permission")


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlRequest:
    """An operator's request for one Run control, before it is addressed.

    Attributes:
        target: The Run's public key.
        control: The control asked for.
    """

    target: str
    control: ControlKind


@dataclass(frozen=True, slots=True, kw_only=True)
class SettingRequest:
    """An operator's edit of one settings key at one layer, before it is addressed.

    Attributes:
        target: The dotted key, as the catalog names it.
        layer: The file layer the edit writes to: the lens it was made under.
        value: The typed value to write; ignored when ``unset``.
        unset: Whether the edit removes the layer's value instead of writing one.
        branch: The branch whose layer a ``branch`` edit writes; ``None`` for the others.
        together: The other keys written in the same write, with their values, for keys
            only valid together; removed with ``target`` when ``unset``.
    """

    target: str
    layer: str
    value: Any = None
    unset: bool = False
    branch: str | None = None
    together: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Refuse an edit no layer write could carry.

        Raises:
            ValueError: ``layer`` is not a file layer, ``target`` names no key, or a
                ``branch`` edit names no branch.
        """
        if self.layer not in SETTING_LAYERS:
            raise ValueError(
                f"layer {self.layer!r} is not one of {', '.join(sorted(SETTING_LAYERS))}"
            )
        if not self.target.strip(".") or ".." in self.target:
            raise ValueError(f"{self.target!r} is not a dotted settings key")
        if self.layer == "branch" and not self.branch:
            raise ValueError("a branch edit needs the branch whose layer it writes")


@dataclass(frozen=True, slots=True, kw_only=True)
class LifecycleRequest:
    """An operator's confirmed lifecycle move, addressed at the revision it was previewed at.

    Attributes:
        target: The record's public key.
        method: The per-entity daemon verb that names the move.
        revision: The revision the consequence card was bound to; the daemon refuses the
            move when the record has moved on, rather than applying it to a revision the
            operator never saw.
        operation_id: The id the card minted, which the daemon files the write under.
        updates: The fields the card stamped at confirmation.
    """

    target: str
    method: str
    revision: int
    operation_id: str
    updates: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a move no lifecycle verb carries.

        Raises:
            ValueError: ``method`` is no lifecycle verb, ``revision`` is not positive, or
                ``operation_id`` is blank.
        """
        if self.method not in MUTATIONS_BY_METHOD:
            raise ValueError(f"{self.method!r} is not a lifecycle verb")
        if self.revision < 1:
            raise ValueError(f"revision must be positive, got {self.revision}")
        if not self.operation_id.strip():
            raise ValueError("a lifecycle move needs the operation id its card minted")


@dataclass(frozen=True, slots=True, kw_only=True)
class NoticeRequest:
    """An operator's disposition of one budget notice, at the revision they were shown.

    Attributes:
        target: The notice's key.
        disposition: ``snooze`` keeps it out of this principal's inbox until
            ``snooze_until``; ``acknowledge`` records that this principal took it in and
            resolves nothing; ``resolve`` closes it for its whole audience.
        revision: The revision the operator was shown; the ledger refuses a disposition
            of a revision the notice has escalated past.
        snooze_until: When a snooze lapses; ``None`` for a resolve.
    """

    target: str
    disposition: Literal["snooze", "acknowledge", "resolve"]
    revision: int
    snooze_until: datetime | None = None

    def __post_init__(self) -> None:
        """Refuse a disposition the ledger would refuse.

        Raises:
            ValueError: ``revision`` is not positive, or a deadline is given exactly when
                the disposition is not a snooze.
        """
        if self.revision < 1:
            raise ValueError(f"revision must be positive, got {self.revision}")
        if (self.disposition == "snooze") != (self.snooze_until is not None):
            raise ValueError("a snooze names when it lapses, and only a snooze does")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionDisposition:
    """An operator's snooze or assignment of one pending action, before it is addressed.

    Attributes:
        target: The pending action's public key.
        verb: ``snooze`` hides it from this operator alone until ``snooze_until``;
            ``assign`` addresses it to ``assignee``.
        snooze_until: When a snooze lapses; ``None`` for an assignment.
        assignee: The principal an assignment addresses it to; ``None`` for a snooze.
    """

    target: str
    verb: Literal["snooze", "assign"]
    snooze_until: datetime | None = None
    assignee: str | None = None

    def __post_init__(self) -> None:
        """Refuse a disposition the daemon would refuse.

        Raises:
            ValueError: A snooze names no deadline, or an assignment names nobody.
        """
        if (self.verb == "snooze") != (self.snooze_until is not None):
            raise ValueError("a snooze names when it lapses, and only a snooze does")
        if (self.verb == "assign") != (self.assignee is not None):
            raise ValueError("an assignment names who it addresses, and only an assignment does")


@dataclass(frozen=True, slots=True, kw_only=True)
class CampaignDrop:
    """An operator's drop of one active Campaign, before it is addressed.

    Attributes:
        target: The Campaign's public key.
    """

    target: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetDate:
    """An operator's proposed target date for one Milestone, before it is addressed.

    Attributes:
        target: The Milestone's public key.
        target_date: The calendar day the Milestone is aimed at.
    """

    target: str
    target_date: date


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchRequest:
    """An operator's pause, drain or resume of the dispatch scheduler.

    Attributes:
        verb: ``pause``, ``drain`` or ``resume``, as the daemon names it.
        target: What the request is about: the tree's one dispatch queue.
    """

    verb: Literal["pause", "drain", "resume"]
    target: str = DISPATCH_QUEUE_TARGET


VerbRequest = (
    AnswerRequest
    | QuestionAnswer
    | ActionDisposition
    | PermissionDecision
    | ControlRequest
    | SettingRequest
    | LifecycleRequest
    | NoticeRequest
    | DispatchRequest
    | CampaignDrop
    | TargetDate
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Operator:
    """Who the console acts as.

    Attributes:
        principal: The principal key every write is attributed to; an answer is sealed
            in this person's name, so it is typed human on the wire.
        receipt_ref: The evidence row an answer is recorded under. The daemon seals only
            an answer that cites a held evidence row, so a console with none refuses to
            answer rather than inventing one; and a row records one answer, so it is
            spent on the first pending action it answers.
    """

    principal: str
    receipt_ref: str | None = None


class OperationStatus(StrEnum):
    """Where one operation stands in the console's ledger: still owed an answer, or closed.

    This is the ledger's own bookkeeping. What the operator is told the request came to
    is the result's :class:`~eawf.kernel.runtime.control.ControlDisposition`, which keeps
    the nine outcomes apart where this collapses them.
    """

    OUTSTANDING = "outstanding"
    APPLIED = "applied"
    SUPERSEDED = "superseded"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsoleOperation:
    """One write, addressed and named, exactly as it is sent.

    Attributes:
        operation_id: The id the daemon files the write under; the same id sent again
            answers the first write rather than making a second.
        method: The daemon verb.
        params: The request parameters, the operation id among them.
        target: The public key of the record written.
    """

    operation_id: str
    method: str
    params: Mapping[str, Any] = field(default_factory=dict)
    target: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationResult:
    """What became of one operation.

    Attributes:
        operation_id: The operation's id; ``None`` for a request refused before it was
            addressed, which was never sent.
        target: The public key of the record the request was about.
        status: Where the operation stands in the ledger.
        detail: One sentence an operator reads.
        disposition: Which of the nine control outcomes the request came to. A request
            refused before it was sent is ``idle``: nothing was asked, so the control
            still stands where it did.
        revision: The revision the daemon answered at: the record's revision after an
            applied move, or the revision a superseding answer stands at.
        answered_by: The principal whose answer won, for a superseded answer.
    """

    operation_id: str | None
    target: str
    status: OperationStatus
    detail: str
    disposition: ControlDisposition
    revision: int | None = None
    answered_by: str | None = None


def _minted(prefix: str) -> str:
    """Return a fresh operation id under ``prefix``."""
    return f"{prefix}-{secrets.token_hex(_ID_BYTES)}"


def mint_lifecycle_id() -> str:
    """Return a fresh operation id for one lifecycle move a consequence card previews."""
    return _minted("MUT")


def address_lifecycle(
    request: LifecycleRequest, *, urn: str, operator: Operator
) -> ConsoleOperation:
    """Return the per-entity verb call ``request`` is sent as.

    The operation id doubles as the daemon's idempotency key, so a repeat of the same
    confirmed card replays the first write's receipt rather than moving the record twice.

    Args:
        request: The confirmed move.
        urn: The record's canonical address, as the projection states it.
        operator: Who the console acts as.

    Returns:
        The addressed operation under the card's own id.
    """
    params: dict[str, Any] = {
        "urn": urn,
        "expected_revision": request.revision,
        "idempotency_key": request.operation_id,
        "actor": operator.principal,
    }
    if request.updates:
        params["updates"] = dict(request.updates)
    return ConsoleOperation(
        operation_id=request.operation_id,
        method=request.method,
        params=MappingProxyType(params),
        target=request.target,
    )


def address_notice(request: NoticeRequest, *, operator: Operator) -> ConsoleOperation:
    """Return the notice-ledger call ``request`` is sent as.

    Args:
        request: The confirmed snooze or resolve.
        operator: Who the console acts as; the ledger records the disposition as theirs.

    Returns:
        The addressed operation under a freshly minted id.
    """
    params: dict[str, Any] = {
        "notice_key": request.target,
        "principal": operator.principal,
        "disposition": request.disposition,
        "expected_revision": request.revision,
    }
    if request.snooze_until is not None:
        params["snooze_until"] = request.snooze_until.isoformat()
    return ConsoleOperation(
        operation_id=_minted("NTC"),
        method=NOTICE_DISPOSE_METHOD,
        params=MappingProxyType(params),
        target=request.target,
    )


def address_dispatch(request: DispatchRequest, *, operator: Operator) -> ConsoleOperation:
    """Return the dispatch control call ``request`` is sent as, under a fresh request id.

    Args:
        request: The confirmed pause, drain or resume.
        operator: Who the console acts as; the daemon records the request as theirs.
    """
    ref = _minted("DSP")
    return ConsoleOperation(
        operation_id=ref,
        method=DISPATCH_CONTROL_METHOD,
        params=MappingProxyType(
            {"verb": request.verb, "actor": operator.principal, "request_ref": ref}
        ),
        target=request.target,
    )


def address_setting(request: SettingRequest) -> ConsoleOperation:
    """Return the layered-config write ``request`` is sent as.

    Config carries no record revision, so a setting is addressed by its key and layer
    alone; the operation id doubles as the daemon's idempotency key, so the reconnect
    asking again under it replays the first write rather than making a second.

    Args:
        request: The edit the operator confirmed.

    Returns:
        The addressed set or unset operation under a freshly minted id.
    """
    key = _minted("CFG")
    params: dict[str, Any] = {"layer": request.layer, "idempotency_key": key}
    if request.together:
        leaves = ((request.target, request.value), *request.together)
        params["writes"] = [
            {"key_path": dotted.split("."), "unset": True}
            if request.unset
            else {"key_path": dotted.split("."), "value": value}
            for dotted, value in leaves
        ]
        method = SETTING_SET_MANY_METHOD
    else:
        params["key_path"] = request.target.split(".")
        if not request.unset:
            params["value"] = request.value
        method = SETTING_UNSET_METHOD if request.unset else SETTING_SET_METHOD
    if request.branch is not None:
        params["branch"] = request.branch
    return ConsoleOperation(
        operation_id=key,
        method=method,
        params=MappingProxyType(params),
        target=request.target,
    )


def address(
    request: AnswerRequest
    | QuestionAnswer
    | ActionDisposition
    | PermissionDecision
    | ControlRequest
    | CampaignDrop
    | TargetDate,
    *,
    urn: str,
    revision: int,
    operator: Operator,
) -> ConsoleOperation | OperationResult:
    """Return the operation ``request`` is sent as, or why it cannot be sent.

    Args:
        request: What the operator asked for.
        urn: The target record's canonical address, as the projection states it.
        revision: The target record's revision, as the projection states it.
        operator: Who the console acts as.

    Returns:
        The addressed operation under a freshly minted id; a refused result when the
        operator holds no receipt an answer could be recorded under.
    """
    if isinstance(request, ControlRequest):
        ref = _minted("CTL")
        return ConsoleOperation(
            operation_id=ref,
            method=CONTROL_METHOD,
            params=MappingProxyType(
                {
                    "urn": urn,
                    "control_request_ref": ref,
                    "control": request.control.value,
                    "actor": operator.principal,
                }
            ),
            target=request.target,
        )
    if isinstance(request, TargetDate):
        key = _minted("TGT")
        return ConsoleOperation(
            operation_id=key,
            method=MILESTONE_TARGET_METHOD,
            params=MappingProxyType(
                {
                    "urn": urn,
                    "expected_revision": revision,
                    "idempotency_key": key,
                    "actor": operator.principal,
                    "target_date": request.target_date.isoformat(),
                }
            ),
            target=request.target,
        )
    if isinstance(request, CampaignDrop):
        return ConsoleOperation(
            operation_id=_minted("CAM"),
            method=CAMPAIGN_CLOSE_METHOD,
            params=MappingProxyType(
                {
                    "urn": urn,
                    "expected_revision": revision,
                    "actor": operator.principal,
                    "to_status": "cancelled",
                    "reason": DROP_REASON,
                }
            ),
            target=request.target,
        )
    if isinstance(request, QuestionAnswer):
        said = (
            {"option_key": request.option_key}
            if request.reply is None
            else {"reply": request.reply}
        )
        return ConsoleOperation(
            operation_id=_minted("QST"),
            method=QUESTION_ANSWER_METHOD,
            params=MappingProxyType(
                {"urn": urn, "expected_revision": revision, "actor": operator.principal, **said}
            ),
            target=request.target,
        )
    if isinstance(request, ActionDisposition):
        key = _minted("ACT")
        params: dict[str, Any] = {
            "urn": urn,
            "expected_revision": revision,
            "idempotency_key": key,
            "actor": operator.principal,
        }
        if request.snooze_until is not None:
            params["snooze_until"] = request.snooze_until.isoformat()
        if request.assignee is not None:
            params["assignee"] = request.assignee
        return ConsoleOperation(
            operation_id=key,
            method=ACTION_VERBS[request.verb],
            params=MappingProxyType(params),
            target=request.target,
        )
    if isinstance(request, PermissionDecision):
        return ConsoleOperation(
            operation_id=_minted("PRM"),
            method=PERMISSION_DECIDE_METHOD,
            params=MappingProxyType(
                {
                    "urn": urn,
                    "verb": request.verb,
                    "principal_class": CONSOLE_PRINCIPAL_CLASS,
                    "actor": operator.principal,
                    "expected_revision": revision,
                }
            ),
            target=request.target,
        )
    if operator.receipt_ref is None:
        return not_sent(request.target, "no evidence receipt to record the answer under")
    key = _minted("console")
    return ConsoleOperation(
        operation_id=key,
        method=SEAL_METHOD,
        params=MappingProxyType(
            {
                "urn": urn,
                "expected_revision": revision,
                "idempotency_key": key,
                "actor": operator.principal,
                "resolver": {"principal_kind": "human", "principal_id": operator.principal},
                "option_id": request.option_id,
                "receipt_ref": operator.receipt_ref,
            }
        ),
        target=request.target,
    )


def rereads(request: VerbRequest, result: OperationResult) -> bool:
    """Return whether the rows holding ``request``'s target are read again after ``result``.

    A stale compare-and-swap is read again so the record is shown as it now stands; an
    answer to a question is, because a host's question lands on a ledger no patch carries;
    and so is a pending action's snooze or assignment, because a snooze moves no revision
    and no patch would redraw it.

    Args:
        request: What the operator asked for.
        result: What became of it.
    """
    if result.status is OperationStatus.REFUSED:
        return f"{STALE_REVISION_CODE}:" in result.detail
    applied = result.status is OperationStatus.APPLIED
    return isinstance(request, QuestionAnswer | ActionDisposition) and applied


def settled(operation: ConsoleOperation, answer: Mapping[str, Any]) -> OperationResult:
    """Return the result the daemon's answer to ``operation`` states.

    A settings write and a sealed answer are facts the daemon wrote, so they are
    confirmed. A Run control is answered with the disposition of the control fact that
    now stands, which for a fresh request is ``requesting``: the daemon recorded that a
    principal asked, and the Run has not moved; an answer naming no disposition leaves
    the outcome unknown rather than assumed. An answer the seal reports as
    ``superseded`` lost to one already given; it is neither applied nor refused, and the
    console says so rather than claiming it applied.

    Args:
        operation: The sent operation the answer is for.
        answer: The daemon's answer payload.

    Returns:
        The result, carrying the disposition the answer states.
    """
    if operation.method in (SETTING_SET_METHOD, SETTING_UNSET_METHOD, SETTING_SET_MANY_METHOD):
        return OperationResult(
            operation_id=operation.operation_id,
            target=operation.target,
            status=OperationStatus.APPLIED,
            detail=_setting_detail(operation, answer),
            disposition=ControlDisposition.CONFIRMED,
        )
    if operation.method in MUTATIONS_BY_METHOD or operation.method == MILESTONE_TARGET_METHOD:
        return _lifecycle_settled(operation, answer)
    if operation.method == CAMPAIGN_CLOSE_METHOD:
        record = answer.get("record")
        stated = record.get("status") if isinstance(record, Mapping) else None
        return OperationResult(
            operation_id=operation.operation_id,
            target=operation.target,
            status=OperationStatus.APPLIED,
            detail=f"{operation.target} dropped · the campaign is "
            f"{str(stated).lower() if stated else 'closed'} · promoted findings stay promoted",
            disposition=ControlDisposition.CONFIRMED,
        )
    if operation.method == NOTICE_DISPOSE_METHOD:
        notice = answer.get("notice")
        stated = notice.get("status") if isinstance(notice, Mapping) else None
        return OperationResult(
            operation_id=operation.operation_id,
            target=operation.target,
            status=OperationStatus.APPLIED,
            detail=f"{operation.target} {operation.params['disposition']}d · the notice is "
            f"{str(stated).lower() if stated else 'recorded'} · no work was stopped",
            disposition=ControlDisposition.CONFIRMED,
        )
    reason = answer.get("reason")
    if answer.get("outcome") == OperationStatus.SUPERSEDED.value:
        disposition = ControlDisposition.SUPERSEDED
    elif operation.method in (
        SEAL_METHOD,
        PERMISSION_DECIDE_METHOD,
        QUESTION_ANSWER_METHOD,
        *ACTION_VERBS.values(),
    ):
        disposition = ControlDisposition.CONFIRMED
    else:
        # an answer that names no disposition says nothing about the effect
        stated = answer.get("disposition")
        disposition = ControlDisposition(stated) if stated else ControlDisposition.UNKNOWN
    detail = outcome_detail(operation, disposition)
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=(
            OperationStatus.SUPERSEDED
            if disposition is ControlDisposition.SUPERSEDED
            else OperationStatus.APPLIED
        ),
        detail=f"{detail} · {reason}" if reason else detail,
        disposition=disposition,
        revision=answer.get("revision") if isinstance(answer.get("revision"), int) else None,
        answered_by=_winner(answer) if disposition is ControlDisposition.SUPERSEDED else None,
    )


#: What each control outcome tells the operator, after the target and the verb. Every
#: sentence says what became of the request and never paints an effect the daemon did
#: not confirm: ``accepted`` is a fact about the request channel, ``rejected`` ends the
#: request and not the target, and ``superseded`` is a loss, never a refusal or an error.
OUTCOME_SENTENCES: Final[Mapping[ControlDisposition, str]] = MappingProxyType(
    {
        ControlDisposition.IDLE: "not requested · the control stands where it did",
        ControlDisposition.REQUESTING: "requested · nothing has moved until the daemon acts",
        ControlDisposition.ACCEPTED: "accepted · the effect is not observed yet",
        ControlDisposition.CONFIRMED: "confirmed · the effect is recorded",
        ControlDisposition.REJECTED: "rejected · the request ended and the target is unchanged",
        ControlDisposition.INVALIDATED: "invalidated · the proof it was bound to changed",
        ControlDisposition.UNKNOWN: "outcome unknown · no answer yet, a reconnect asks again",
        ControlDisposition.RECOVERY: "recovery · asking again found no answer, you decide",
        ControlDisposition.SUPERSEDED: "superseded · an answer already given stands, "
        "nothing was written for this one",
    }
)


def _check_outcome_sentences() -> None:
    """Refuse a disposition the console has no sentence for.

    Raises:
        ValueError: A disposition is missing, so a result carrying it would be announced
            as whatever the renderer fell back to.
    """
    missing = sorted(value.value for value in ControlDisposition if value not in OUTCOME_SENTENCES)
    if missing:
        raise ValueError(f"no outcome sentence for disposition {', '.join(missing)}")


_check_outcome_sentences()


def outcome_detail(operation: ConsoleOperation, disposition: ControlDisposition) -> str:
    """Return the sentence an operator reads for ``operation`` reaching ``disposition``.

    Args:
        operation: The sent operation.
        disposition: What it came to.

    Returns:
        The target, the verb asked for, and the outcome's own sentence.
    """
    params = operation.params
    verb = (
        params.get("control")
        or params.get("option_id")
        or params.get("option_key")
        or params.get("verb")
        or ("reply" if "reply" in params else "request")
    )
    return f"{operation.target} {verb} {OUTCOME_SENTENCES[disposition]}"


def _winner(answer: Mapping[str, Any]) -> str | None:
    """Return the principal whose answer sealed the action, as the seal's dispositions name."""
    rows = answer.get("dispositions") or ()
    return next(
        (
            str(row["principal_id"])
            for row in rows
            if isinstance(row, Mapping) and row.get("outcome") == "sealed"
        ),
        None,
    )


def _lifecycle_settled(operation: ConsoleOperation, answer: Mapping[str, Any]) -> OperationResult:
    """Return what a per-entity verb's envelope states: committed, or refused with its code.

    A refusal is answered in the envelope rather than as a transport error, so its code,
    the guard that failed and the remediation are read off the envelope's first error row.
    A committed lifecycle move names the status it reached; a target date names the day.
    """
    after = answer.get("revision_after")
    revision = after if isinstance(after, int) else None
    errors = answer.get("errors") or ()
    if answer.get("status") != "ok":
        first = errors[0] if errors and isinstance(errors[0], Mapping) else {}
        guard = f" · guard {first['guard']}" if first.get("guard") else ""
        return OperationResult(
            operation_id=operation.operation_id,
            target=operation.target,
            status=OperationStatus.REFUSED,
            disposition=ControlDisposition.REJECTED,
            detail=(
                f"{operation.target} refused · {first.get('code', 'refused')}{guard} · "
                f"{first.get('remediation', 'nothing was written')}"
            ),
            revision=revision,
        )
    mutation = MUTATIONS_BY_METHOD.get(operation.method)
    moved = (
        mutation.to_status
        if mutation is not None
        else f"target date {operation.params['target_date']}"
    )
    before = answer.get("revision_before")
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.APPLIED,
        disposition=ControlDisposition.CONFIRMED,
        detail=f"{operation.target} {moved} · revision {before} → {after} · committed",
        revision=revision,
    )


def _setting_detail(operation: ConsoleOperation, answer: Mapping[str, Any]) -> str:
    """Return what a layered-config answer says was written, in the operator's words."""
    layer = answer.get("layer", operation.params.get("layer"))
    if operation.method == SETTING_SET_MANY_METHOD:
        keys = ", ".join(".".join(write["key_path"]) for write in operation.params["writes"])
        done = "unset" if operation.params["writes"][0].get("unset") else "written"
        return f"{keys} {done} at {layer} · settings re-read"
    if operation.method == SETTING_UNSET_METHOD:
        if answer.get("removed") is False:
            return f"{operation.target} was not set at {layer} · nothing was written"
        return f"{operation.target} unset at {layer} · settings re-read"
    return f"{operation.target} written at {layer} · settings re-read"


def refused(operation: ConsoleOperation, message: str) -> OperationResult:
    """Return the result of a write the daemon answered with a refusal; nothing was written.

    Args:
        operation: The sent operation that was refused.
        message: The daemon's refusal message.

    Returns:
        A refused result carrying the message.
    """
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.REFUSED,
        detail=f"{outcome_detail(operation, ControlDisposition.REJECTED)} · {message}",
        disposition=ControlDisposition.REJECTED,
    )


def unanswered(operation: ConsoleOperation) -> OperationResult:
    """Return the result of a write whose answer never arrived; its outcome is unknown.

    Args:
        operation: The sent operation still waiting for an answer.

    Returns:
        An outstanding result a reconnect later reconciles by the operation's id.
    """
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.OUTSTANDING,
        detail=outcome_detail(operation, ControlDisposition.UNKNOWN),
        disposition=ControlDisposition.UNKNOWN,
    )


def exhausted(operation: ConsoleOperation) -> OperationResult:
    """Return the result of a write whose answer was lost again when a reconnect asked.

    The reconnect is the console's one automatic reconciliation, so once it too goes
    unanswered the effect stays undetermined and the operator decides; the operation
    stays in the ledger so a later reconnect can still settle it.

    Args:
        operation: The sent operation the reconciliation asked about.

    Returns:
        An outstanding result in ``recovery``.
    """
    return OperationResult(
        operation_id=operation.operation_id,
        target=operation.target,
        status=OperationStatus.OUTSTANDING,
        detail=outcome_detail(operation, ControlDisposition.RECOVERY),
        disposition=ControlDisposition.RECOVERY,
    )


def not_sent(target: str, reason: str) -> OperationResult:
    """Return the result of a request refused before it was addressed; nothing was sent.

    Args:
        target: The public key of the record the request was about.
        reason: Why the console could not send it.

    Returns:
        A refused result in ``idle``: no request exists, so the control stands where it did.
    """
    return OperationResult(
        operation_id=None,
        target=target,
        status=OperationStatus.REFUSED,
        detail=f"{reason} — nothing was sent",
        disposition=ControlDisposition.IDLE,
    )


class OperationLedger:
    """The operations sent and not yet answered, in the order they were sent.

    The ledger is what the reconnect protocol reconciles: an operation stays in it from
    the moment it is sent until an answer for its id arrives, however many breaks in the
    link happen in between.
    """

    def __init__(self) -> None:
        self._outstanding: dict[str, ConsoleOperation] = {}

    def open(self, operation: ConsoleOperation) -> None:
        """Hold ``operation`` as sent and not yet answered.

        Args:
            operation: The operation just sent.

        Raises:
            ValueError: An operation with the same id is already outstanding.
        """
        if operation.operation_id in self._outstanding:
            raise ValueError(f"operation {operation.operation_id} is already outstanding")
        self._outstanding[operation.operation_id] = operation

    def settle(self, result: OperationResult) -> None:
        """Close the operation ``result`` answers; an outstanding result keeps it open.

        Args:
            result: The answer for one outstanding operation.

        Raises:
            KeyError: No outstanding operation carries the result's id.
        """
        if result.operation_id is None or result.operation_id not in self._outstanding:
            raise KeyError(f"no outstanding operation {result.operation_id}")
        if result.status is not OperationStatus.OUTSTANDING:
            del self._outstanding[result.operation_id]

    def outstanding(self) -> tuple[ConsoleOperation, ...]:
        """Return every operation still waiting for an answer, oldest first."""
        return tuple(self._outstanding.values())


__all__ = [
    "ACTION_ASSIGN_METHOD",
    "ACTION_SNOOZE_METHOD",
    "ACTION_VERBS",
    "ANSWER_OPTIONS",
    "CAMPAIGN_CLOSE_METHOD",
    "CAMPAIGN_ROUTE",
    "CONTROL_METHOD",
    "DISMISS_REPLY",
    "DISMISS_VERB",
    "DISPATCH_CONTROL_METHOD",
    "DISPATCH_KINDS",
    "DISPATCH_QUEUE_TARGET",
    "DISPATCH_VERBS",
    "DROP_CAMPAIGN_VERB",
    "DROP_REASON",
    "MILESTONE_TARGET_METHOD",
    "NOTICE_DISPOSE_METHOD",
    "NOTICE_LIST_METHOD",
    "NO_PRINCIPAL_REASON",
    "OUTCOME_SENTENCES",
    "PERMISSION_DECIDE_METHOD",
    "PERMISSION_VERBS",
    "PROPOSE_DATE_VERB",
    "QUESTION_ANSWER_METHOD",
    "QUESTION_OPTIONS",
    "RUN_CONTROLS",
    "RUN_KINDS",
    "SAME_VERB",
    "SEAL_METHOD",
    "SETTINGS_ROUTE",
    "SETTING_LAYERS",
    "SETTING_SET_MANY_METHOD",
    "SETTING_SET_METHOD",
    "SETTING_UNSET_METHOD",
    "SETTING_VERBS",
    "SNOOZE_FOR",
    "STALE_REVISION_CODE",
    "TIMELINE_ROUTE",
    "UNBOUND_REASON",
    "ActionDisposition",
    "AnswerRequest",
    "CampaignDrop",
    "ConsoleOperation",
    "ControlRequest",
    "DispatchRequest",
    "LifecycleRequest",
    "NoticeRequest",
    "OperationLedger",
    "OperationResult",
    "OperationStatus",
    "Operator",
    "PermissionDecision",
    "QuestionAnswer",
    "SettingRequest",
    "TargetDate",
    "VerbRequest",
    "address",
    "address_dispatch",
    "address_lifecycle",
    "address_notice",
    "address_setting",
    "binding_refusal",
    "exhausted",
    "linked_refusal",
    "mint_lifecycle_id",
    "not_sent",
    "outcome_detail",
    "refused",
    "rereads",
    "settled",
    "unanswered",
]
