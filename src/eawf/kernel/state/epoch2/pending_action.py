"""The queued question a person has to answer, and who is allowed to answer it.

A pending action is the record of a decision the machine deliberately did
not take. Two of its rules are structural rather than checked, because a
check is only as good as the call site that remembers to make it.

Who resolves. :attr:`PendingAction.resolution_actor` is typed
:class:`HumanPrincipal`, and :class:`HumanPrincipal` and
:class:`AgentPrincipal` are two closed shapes discriminated on
``principal_kind``. An agent therefore cannot be written into the
resolver slot at all: the loader refuses the agent discriminator, and an
agent spelled as a human is refused too, because the agent shape carries
the Run it acts inside and the human shape forbids unknown keys. The
result is that "an agent approved it" is not a state this model can hold,
rather than a state some validator is trusted to notice.

What a timeout may do. An option chosen because nobody answered is an
answer nobody gave. That is tolerable for an operator's routing choice
and never for a protected approval, so :attr:`PendingAction.kind` decides
whether :attr:`PendingAction.default_on_timeout` may be set at all, and a
protected approval carrying one does not validate.

The record is minimal on purpose: what is being asked, the two-to-four
answers offered, the exact digest the answer is bound to, and the seal.
Rendering the question, dispatching it to a surface, and filing the
sealed row are not here.

A second answer. Two principals can each be the one who completes an
already-sealed question -- a race, not a mistake by either of them -- so
:meth:`PendingAction.answer` never reports the loser's request as
rejected. It reports :attr:`AnswerOutcome.SUPERSEDED` and the choice that
won, and it records the loser's own disposition beside the winner's
seal, because "who else answered, and with what" is the fact an operator
needs and a denial does not carry. The identical winning answer retried
is idempotent and changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import (
    EntityKind,
    IdentityError,
    QualifiedUrn,
    parse_qualified_urn,
    validate_entity_key,
)
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    Sha256DigestStr,
    StrictPositiveInt,
    TitleStr,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn, PendingActionUrn, RunUrn
from eawf.kernel.state.types import UtcDatetime


def _validate_action_key(value: str) -> str:
    """Admit only a canonical ``ACT-####`` pending-action key."""
    return validate_entity_key(EntityKind.PENDING_ACTION, value)


#: An ``ACT-####`` pending-action key. The grammar has one home in the
#: identity package, so the alias delegates rather than re-spelling it.
PendingActionKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_action_key),
]

#: The caller's name for one offered answer. Options are compared by id
#: and displayed by label, so the id grammar is deliberately narrow.
OptionId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,31}$")]

#: The fewest answers a question may offer. One option is not a question.
MIN_OPTIONS: Final = 2

#: The most answers a question may offer. Past four, a surface stops
#: being able to show them side by side and starts summarising them,
#: which is where an operator answers a paraphrase of the question.
MAX_OPTIONS: Final = 4


class _FrozenModel(Epoch2Model):
    """Strict and immutable: a sealed answer edited afterwards is not an answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HumanPrincipal(_FrozenModel):
    """A person, by immutable qualified key.

    There is no Run field and no unknown key is admitted, so this shape
    cannot carry an agent that spelled itself human.
    """

    principal_kind: Literal["human"]
    principal_id: PrincipalKey


class AgentPrincipal(_FrozenModel):
    """An agent, by immutable qualified key and the Run it acts inside.

    The Run reference is required because an agent only ever acts inside
    one: an agent principal with no Run is an agent nobody dispatched.
    """

    principal_kind: Literal["agent"]
    principal_id: PrincipalKey
    run_ref: RunUrn


#: Who may ask a question. Either kind may open one -- an agent that
#: reaches a decision it is not allowed to take asks the operator.
ActionPrincipal = Annotated[HumanPrincipal | AgentPrincipal, Field(discriminator="principal_kind")]


class PendingActionKind(StrEnum):
    """What sort of answer the question is asking for.

    The kind is not a label: it decides whether a timeout may answer on
    the operator's behalf, and whether the answer has to be bound to an
    exact digest.
    """

    PROTECTED_APPROVAL = "protected_approval"
    OPERATOR_DECISION = "operator_decision"


#: The kinds no timeout may answer. An approval that lapses into a yes is
#: the approval nobody gave, which is the one failure this record exists
#: to make impossible.
UNDEFAULTABLE_KINDS: Final[frozenset[PendingActionKind]] = frozenset(
    {PendingActionKind.PROTECTED_APPROVAL}
)

#: The kinds whose answer must name the exact bytes it approves. A yes
#: bound to nothing is a yes to whatever the tree says next.
DIGEST_BOUND_KINDS: Final[frozenset[PendingActionKind]] = frozenset(
    {PendingActionKind.PROTECTED_APPROVAL}
)


class OptionEffect(StrEnum):
    """What choosing one option does to the thing being asked about."""

    APPROVE = "approve"
    DECLINE = "decline"
    REQUEST_REPAIR = "request_repair"


class AnswerOutcome(StrEnum):
    """What one answer to a pending action achieved.

    Distinct from :class:`PendingActionStatus`: the status is the
    record's own state and stays whatever it already was, while the
    outcome is what this particular answer, from this particular
    principal, got out of asking. ``SUPERSEDED`` is never a status and
    never moves the action; it is the shape of a loss, not a rejection.
    """

    SEALED = "sealed"
    SUPERSEDED = "superseded"


class PendingActionStatus(StrEnum):
    """The stored states of one queued question.

    ``CREATED`` is a question nobody has been shown yet, ``WAITING`` is
    one that reached a surface, and ``SEALED`` is one a person answered.
    Nothing leaves ``SEALED``: a changed mind is a new question.
    """

    CREATED = "CREATED"
    WAITING = "WAITING"
    SEALED = "SEALED"


_S = PendingActionStatus

#: Every legal status move. A move that is not listed does not exist, so
#: a question cannot reach a seal without having been asked.
PENDING_ACTION_EDGES: Final[Mapping[PendingActionStatus, frozenset[PendingActionStatus]]] = {
    _S.CREATED: frozenset({_S.WAITING}),
    _S.WAITING: frozenset({_S.SEALED}),
    _S.SEALED: frozenset(),
}


class PendingActionOption(_FrozenModel):
    """One answer the question offers, and what choosing it does."""

    option_id: OptionId
    label: TitleStr
    effect: OptionEffect


class PrincipalDispositionRow(_FrozenModel):
    """One principal's own outcome of the action, beside its shared status.

    The record keeps one row per principal, replaced whenever that
    principal answers again, so a principal who answers more than once
    is represented by their latest disposition rather than a growing
    history only the ledger that files the action needs to keep.
    """

    principal_id: PrincipalKey
    outcome: AnswerOutcome
    option_id: OptionId


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """What one answer to a pending action achieved.

    Attributes:
        action: The record after the answer -- the winning seal one
            revision on, a losing disposition recorded beside the
            standing seal, or the same record unchanged when the winning
            answer itself was retried.
        outcome: What this particular answer achieved.
        option_id: The option the record stands sealed with -- the
            caller's own choice when they won, the winner's choice when
            they did not.
        resolution_actor: Who is on file as having resolved the action.
        receipt_ref: The receipt the resolution is on file with.
    """

    action: PendingAction
    outcome: AnswerOutcome
    option_id: OptionId
    resolution_actor: HumanPrincipal
    receipt_ref: EvidenceUrn


class PendingAction(_FrozenModel):
    """One queued question, its offered answers, and the seal of the answer given.

    The three seal fields move together. A row carrying a resolver and no
    receipt claims an answer nothing recorded, and a row carrying a
    receipt and no resolver claims an answer nobody gave; both are
    refused, so ``SEALED`` means all of "who", "which" and "proved by"
    are on file.

    ``revision`` is the compare-and-swap token a seal is decided against,
    so an answer given to a question that has since moved is refused
    rather than landed on top of it. It defaults to one because a row
    nothing has moved yet is at its first revision.

    ``urn`` addresses the row itself, the way every other epoch-2 record
    addresses its own row: a route projection keys and validates every
    row it renders by this field, so a stored row without one refuses
    the whole route's read rather than just its own. A row written
    before the field existed is read the way a fresh write would have
    set it -- :meth:`_urn_defaults_from_subject` derives it from
    ``subject_ref``, which is filed in the same repository -- so an
    older row is still addressable rather than failing to load.

    ``assignee_ref`` names the principal expected to answer. It transfers
    no authority -- anyone eligible may still answer -- and any
    resolution clears it, so a sealed action never carries one.
    ``dispositions`` is the per-principal record beside ``status``: one
    row per principal who has answered, telling a caller who answered
    what without exposing the whole answer race as an error.
    """

    id: PendingActionKey
    urn: PendingActionUrn
    kind: PendingActionKind
    subject_ref: AnyEntityUrn
    question: NonEmptyStr
    bundle_digest: Sha256DigestStr | None = None
    options: tuple[PendingActionOption, ...] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    default_on_timeout: OptionId | None = None
    idempotency_key: IdempotencyKey
    status: PendingActionStatus
    revision: StrictPositiveInt = 1
    requested_by: ActionPrincipal
    resolution_actor: HumanPrincipal | None = None
    selected_option_id: OptionId | None = None
    receipt_ref: EvidenceUrn | None = None
    assignee_ref: PrincipalKey | None = None
    dispositions: tuple[PrincipalDispositionRow, ...] = ()
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="before")
    @classmethod
    def _urn_defaults_from_subject(cls, data: Any) -> Any:
        """Backfill ``urn`` from ``subject_ref`` and ``id`` for a row that carries none.

        A pending action is filed in the same repository as the record its
        question is about, so its own urn is that container's prefix with
        the pending-action kind and its own key swapped in -- the same
        derivation the row's own writer takes before it is ever asked to
        default. Leaving the input alone when a resolvable urn cannot be
        derived is deliberate: it falls through to the field's own
        required-value refusal, which names the field rather than the
        subject that failed to parse.

        Args:
            data: The raw input :meth:`~pydantic.BaseModel.model_validate`
                or the constructor was given.

        Returns:
            *data* unchanged, or a copy with ``urn`` filled in.
        """
        if not isinstance(data, Mapping) or data.get("urn"):
            return data
        key = data.get("id")
        if not isinstance(key, str):
            return data
        subject = data.get("subject_ref")
        if isinstance(subject, QualifiedUrn):
            container = subject
        elif isinstance(subject, str):
            try:
                container = parse_qualified_urn(subject)
            except IdentityError:
                return data
        else:
            return data
        try:
            derived = QualifiedUrn(
                workspace_key=container.workspace_key,
                project_key=container.project_key,
                repository_key=container.repository_key,
                kind=EntityKind.PENDING_ACTION,
                entity_key=key,
            )
        except IdentityError:
            return data
        return {**data, "urn": str(derived)}

    @property
    def option_ids(self) -> tuple[str, ...]:
        """Return every offered answer's id, in declaration order."""
        return tuple(item.option_id for item in self.options)

    @property
    def selected_option(self) -> PendingActionOption | None:
        """Return the option the resolver chose, or ``None`` before the seal."""
        for item in self.options:
            if item.option_id == self.selected_option_id:
                return item
        return None

    @model_validator(mode="after")
    def _options_are_distinct(self) -> Self:
        """Require distinct ids, at most one yes, and a default that is offered.

        Declining has several shapes -- decline and cancel, decline and
        defer -- so the effects are not globally unique. Approving has
        exactly one: a question offering two ways to say yes leaves
        "was this approved" depending on which was read.

        Raises:
            ValueError: Two options share an id, more than one option
                approves, or the timeout default names an option the
                question does not offer.
        """
        ids = [item.option_id for item in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"action {self.id!r} offers the same option id twice")
        approving = sorted(
            item.option_id for item in self.options if item.effect is OptionEffect.APPROVE
        )
        if len(approving) > 1:
            raise ValueError(
                f"action {self.id!r} offers {', '.join(approving)} as approving answers, so "
                "whether it was approved would depend on which was read"
            )
        if self.default_on_timeout is not None and self.default_on_timeout not in ids:
            raise ValueError(
                f"default_on_timeout {self.default_on_timeout!r} is not one of {', '.join(ids)}"
            )
        return self

    @model_validator(mode="after")
    def _kind_carries_what_it_requires(self) -> Self:
        """Refuse a timeout default and require a digest where the kind says so.

        Raises:
            ValueError: A protected approval carries a timeout default,
                or a digest-bound kind names no digest, so the answer
                would not say what it approved.
        """
        if self.kind in UNDEFAULTABLE_KINDS and self.default_on_timeout is not None:
            raise ValueError(
                f"a {self.kind.value} cannot carry default_on_timeout: an approval that lapses "
                "into a yes is the approval nobody gave"
            )
        if self.kind in DIGEST_BOUND_KINDS and self.bundle_digest is None:
            raise ValueError(f"a {self.kind.value} requires the bundle_digest it approves")
        return self

    @model_validator(mode="after")
    def _seal_matches_status(self) -> Self:
        """Require who answered, which answer, and its receipt exactly when sealed.

        Raises:
            ValueError: The seal fields do not all agree with the status,
                the chosen option is not one the question offers, or the
                clock runs backwards.
        """
        sealed = self.status is PendingActionStatus.SEALED
        present = {
            "resolution_actor": self.resolution_actor is not None,
            "selected_option_id": self.selected_option_id is not None,
            "receipt_ref": self.receipt_ref is not None,
        }
        wrong = sorted(name for name, is_set in present.items() if is_set is not sealed)
        if wrong:
            raise ValueError(
                f"{', '.join(wrong)} must be set exactly when the action is "
                f"{PendingActionStatus.SEALED.value}, not in {self.status.value}"
            )
        if sealed and self.selected_option is None:
            raise ValueError(
                f"selected_option_id {self.selected_option_id!r} is not one of "
                f"{', '.join(self.option_ids)}"
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @model_validator(mode="after")
    def _dispositions_are_keyed_by_principal(self) -> Self:
        """Require at most one disposition row per principal.

        Raises:
            ValueError: Two rows name the same principal. A principal has
                one current disposition, not a history of them, so a
                repeated key is a defect in whatever wrote the row rather
                than a second fact worth keeping.
        """
        principals = [item.principal_id for item in self.dispositions]
        if len(set(principals)) != len(principals):
            raise ValueError("dispositions holds more than one row for the same principal")
        return self

    def advance(self, to: PendingActionStatus, *, at: UtcDatetime) -> Self:
        """Return the question one status on, without answering it.

        Args:
            to: The status to move into.
            at: When the move happened.

        Returns:
            The advanced record.

        Raises:
            ValueError: The move is not an edge of the status machine, or
                it would reach the seal without an answer.
        """
        if to not in PENDING_ACTION_EDGES[self.status]:
            raise ValueError(f"action {self.status.value} does not move to {to.value}")
        if to is PendingActionStatus.SEALED:
            raise ValueError(
                f"reaching {PendingActionStatus.SEALED.value} needs the answer that was given; "
                "seal the action instead of advancing it"
            )
        return self.model_validate(
            {**self.model_dump(), "status": to, "revision": self.revision + 1, "updated_at": at}
        )

    def seal(
        self,
        *,
        resolver: HumanPrincipal,
        option_id: str,
        receipt_ref: object,
        at: UtcDatetime,
    ) -> Self:
        """Return the question sealed with the answer a person gave.

        The resolver is typed :class:`HumanPrincipal`, so an agent cannot
        be passed here at all -- which is the whole of the rule rather
        than a check this method performs.

        Args:
            resolver: The person who answered.
            option_id: The answer they chose.
            receipt_ref: The evidence row recording the answer.
            at: When the answer was given.

        Returns:
            The sealed record.

        Raises:
            ValueError: The action is not waiting to be answered, or the
                sealed record breaks a rule -- an option the question does
                not offer, or a clock that runs backwards.
        """
        if self.status is not PendingActionStatus.WAITING:
            raise ValueError(
                f"only a {PendingActionStatus.WAITING.value} action is sealed, not a "
                f"{self.status.value} one"
            )
        return self.model_validate(
            {
                **self.model_dump(),
                "status": PendingActionStatus.SEALED,
                "revision": self.revision + 1,
                "resolution_actor": resolver.model_dump(),
                "selected_option_id": option_id,
                "receipt_ref": receipt_ref,
                "assignee_ref": None,
                "updated_at": at,
            }
        )

    def answer(
        self,
        *,
        expected_revision: int,
        resolver: HumanPrincipal,
        option_id: str,
        receipt_ref: object,
        at: UtcDatetime,
    ) -> AnswerResult:
        """Return the result of one answer: sealing the action, or losing the race.

        The first answer at the action's current revision seals it,
        exactly as :meth:`seal` always has. Every answer that reaches an
        already-sealed action -- a different principal's, the same
        principal choosing differently the second time, or their own
        winning answer retried unchanged -- is not a second attempt at a
        question that is gone: the seal it would have made was already
        made, by this same call or by somebody else's. The retried
        winning answer is idempotent and returns the first receipt
        unchanged; every other one reports :attr:`AnswerOutcome.SUPERSEDED`
        and the winning choice instead of raising.

        Args:
            expected_revision: The revision the answer was given against.
                Checked only while the action is still waiting: an answer
                that finds it already sealed has necessarily read a stale
                revision, which is the race this method exists to settle
                rather than reject.
            resolver: The person who answered.
            option_id: The answer they chose.
            receipt_ref: The evidence row recording the answer, used only
                when this call is the one that seals the action.
            at: When the answer was given.

        Returns:
            The result: what this answer achieved, the record carrying
            this principal's disposition, and the winning choice.

        Raises:
            ValueError: The resolver is not a person, the option named is
                not one the question offers, the action has never been
                asked (``CREATED``), or a still-waiting action is
                answered against a revision it is not at.
        """
        if not isinstance(resolver, HumanPrincipal):
            raise ValueError(f"{self.id} is answered by a person, not {type(resolver).__name__}")
        if option_id not in self.option_ids:
            raise ValueError(f"{self.id} offers {', '.join(self.option_ids)}, not {option_id!r}")
        if self.status is PendingActionStatus.SEALED:
            return self._answer_sealed(resolver=resolver, option_id=option_id)
        if self.status is not PendingActionStatus.WAITING:
            raise ValueError(
                f"{self.id} is {self.status.value}; only a "
                f"{PendingActionStatus.WAITING.value} question can be answered"
            )
        if self.revision != expected_revision:
            raise ValueError(
                f"{self.id} is at revision {self.revision}, not the {expected_revision} the "
                "answer was given against"
            )
        sealed = self.seal(resolver=resolver, option_id=option_id, receipt_ref=receipt_ref, at=at)
        recorded = sealed.with_disposition(
            principal_id=resolver.principal_id, outcome=AnswerOutcome.SEALED, option_id=option_id
        )
        assert recorded.receipt_ref is not None, "seal always sets the receipt"
        return AnswerResult(
            action=recorded,
            outcome=AnswerOutcome.SEALED,
            option_id=option_id,
            resolution_actor=resolver,
            receipt_ref=recorded.receipt_ref,
        )

    def _answer_sealed(self, *, resolver: HumanPrincipal, option_id: str) -> AnswerResult:
        """Return the result of an answer that reaches an already-sealed action."""
        winner = self.resolution_actor
        choice = self.selected_option_id
        receipt = self.receipt_ref
        assert winner is not None, "sealed always carries who resolved it"
        assert choice is not None, "sealed always carries the chosen option"
        assert receipt is not None, "sealed always carries its receipt"
        if resolver.principal_id == winner.principal_id and option_id == choice:
            return AnswerResult(
                action=self,
                outcome=AnswerOutcome.SEALED,
                option_id=choice,
                resolution_actor=winner,
                receipt_ref=receipt,
            )
        recorded = self.with_disposition(
            principal_id=resolver.principal_id,
            outcome=AnswerOutcome.SUPERSEDED,
            option_id=option_id,
        )
        return AnswerResult(
            action=recorded,
            outcome=AnswerOutcome.SUPERSEDED,
            option_id=choice,
            resolution_actor=winner,
            receipt_ref=receipt,
        )

    def with_disposition(
        self, *, principal_id: str, outcome: AnswerOutcome, option_id: str
    ) -> Self:
        """Return this record with *principal_id*'s disposition row set to this answer.

        Dispositions are a per-principal side record, not part of the
        compare-and-swap identity a seal is decided against, so recording
        one does not move ``revision``: a caller that also seals or
        advances the record in the same call folds this in without
        spending a second revision on what is one logical answer. Called
        by :meth:`answer` for both outcomes, and by a daemon method that
        seals a still-waiting action so the winner's own disposition rides
        the same commit as the seal.

        Args:
            principal_id: The principal whose disposition row is replaced.
            outcome: How that principal's answer landed.
            option_id: The option the principal chose.

        Returns:
            A validated copy carrying the replaced disposition row, at the
            same ``revision``.
        """
        row = PrincipalDispositionRow(
            principal_id=principal_id, outcome=outcome, option_id=option_id
        )
        kept = tuple(item for item in self.dispositions if item.principal_id != principal_id)
        return self.model_validate({**self.model_dump(), "dispositions": (*kept, row)})


__all__ = [
    "DIGEST_BOUND_KINDS",
    "MAX_OPTIONS",
    "MIN_OPTIONS",
    "PENDING_ACTION_EDGES",
    "UNDEFAULTABLE_KINDS",
    "ActionPrincipal",
    "AgentPrincipal",
    "AnswerOutcome",
    "AnswerResult",
    "HumanPrincipal",
    "OptionEffect",
    "OptionId",
    "PendingAction",
    "PendingActionKey",
    "PendingActionKind",
    "PendingActionOption",
    "PendingActionStatus",
    "PrincipalDispositionRow",
]
