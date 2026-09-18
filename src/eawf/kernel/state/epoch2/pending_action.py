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
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    Sha256DigestStr,
    TitleStr,
)
from eawf.kernel.state.epoch2.urns import AnyEntityUrn, EvidenceUrn, RunUrn
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


class PendingAction(_FrozenModel):
    """One queued question, its offered answers, and the seal of the answer given.

    The three seal fields move together. A row carrying a resolver and no
    receipt claims an answer nothing recorded, and a row carrying a
    receipt and no resolver claims an answer nobody gave; both are
    refused, so ``SEALED`` means all of "who", "which" and "proved by"
    are on file.
    """

    id: PendingActionKey
    kind: PendingActionKind
    subject_ref: AnyEntityUrn
    question: NonEmptyStr
    bundle_digest: Sha256DigestStr | None = None
    options: tuple[PendingActionOption, ...] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)
    default_on_timeout: OptionId | None = None
    idempotency_key: IdempotencyKey
    status: PendingActionStatus
    requested_by: ActionPrincipal
    resolution_actor: HumanPrincipal | None = None
    selected_option_id: OptionId | None = None
    receipt_ref: EvidenceUrn | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

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
        return self.model_validate({**self.model_dump(), "status": to, "updated_at": at})

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
                "resolution_actor": resolver.model_dump(),
                "selected_option_id": option_id,
                "receipt_ref": receipt_ref,
                "updated_at": at,
            }
        )


__all__ = [
    "DIGEST_BOUND_KINDS",
    "MAX_OPTIONS",
    "MIN_OPTIONS",
    "PENDING_ACTION_EDGES",
    "UNDEFAULTABLE_KINDS",
    "ActionPrincipal",
    "AgentPrincipal",
    "HumanPrincipal",
    "OptionEffect",
    "OptionId",
    "PendingAction",
    "PendingActionKey",
    "PendingActionKind",
    "PendingActionOption",
    "PendingActionStatus",
]
