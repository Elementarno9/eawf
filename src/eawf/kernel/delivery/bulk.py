"""One operator decision over many Runs or Tasks, with one result per item.

A mass abort naming twenty-seven Runs is one decision, and issuing it as
twenty-seven unrelated requests loses that fact and leaves nowhere to say
that twenty-four stopped, two went quiet and one was refused. A
:class:`BulkOperation` keeps both: one record over an explicit item set,
and a ledger per item that is never collapsed into a single flag. The Run
controls name Runs; releasing leases names Tasks, and every item of one
operation is of the kind its verb takes.

Each item walks its own small lifecycle, :data:`ITEM_EDGES`. An item is
``requested`` when it is admitted, ``accepted`` or ``rejected`` once its
own authority and compare-and-swap anchor are judged, and ``confirmed``,
``unknown`` or ``invalidated`` once what happened to it is observed. An
item is only ever moved by an observation, so an effect nobody saw keeps
the item where it was: bulk never fabricates a terminal state.

The operator is shown a :class:`BulkConfirmation` before anything opens:
how many targets, what the verb will do, what it will not do, and what
happens to an item that moved in between. The operation carries the
digest of the confirmation it was opened under, so an operation cannot be
opened against a consequence nobody was shown.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.runtime.control import ControlDisposition, ControlRequestId
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    PrincipalKey,
    Sha256DigestStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import BulkItemUrn


class _FrozenModel(Epoch2Model):
    """Strict and immutable: a per-item result edited after the fact says nothing."""

    model_config = ConfigDict(extra="forbid", frozen=True)


#: The client's name for one bulk operation.
BulkIdempotencyKey = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]

#: The stable code a refused item carries, in the daemon's refusal vocabulary.
RefusalCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class BulkVerb(StrEnum):
    """The closed set of verbs an operation may apply to many items at once.

    Only recoverable control is bulk-eligible: a Run control whose effect
    the Run's driver observes, or the release of a Task's lease, which
    plans the Task again. Integration, merge, acceptance and publication
    never are, because a partial outcome of any of them is not something
    an operator can simply reissue. ``release`` is the Task lease release,
    not a publication.
    """

    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    RETRY = "retry"
    RELEASE = "release"


#: The kind of record each verb names, so a Run control never addresses a
#: Task and a release never addresses a Run.
BULK_ITEM_KINDS: Final[Mapping[BulkVerb, EntityKind]] = MappingProxyType(
    {
        BulkVerb.CANCEL: EntityKind.RUN,
        BulkVerb.INTERRUPT: EntityKind.RUN,
        BulkVerb.RETRY: EntityKind.RUN,
        BulkVerb.RELEASE: EntityKind.TASK,
    }
)

#: The Run control each Run bulk verb asks for, item by item.
BULK_CONTROLS: Final[Mapping[BulkVerb, ControlKind]] = MappingProxyType(
    {
        BulkVerb.CANCEL: ControlKind.CANCEL,
        BulkVerb.INTERRUPT: ControlKind.INTERRUPT,
        BulkVerb.RETRY: ControlKind.RETRY,
    }
)

#: What each verb does to one item, and what it leaves alone. The
#: non-effects are stated because the surprising consequence of a mass
#: control is usually the one it does not have.
_CONSEQUENCES: Final[Mapping[BulkVerb, tuple[tuple[str, ...], tuple[str, ...]]]] = MappingProxyType(
    {
        BulkVerb.CANCEL: (
            (
                "asks each Run's driver to stop it",
                "a Run ends CANCELLED only once its driver observes the stop",
            ),
            (
                "does not release the Runs' Tasks, which keep their status and lease",
                "does not change any Batch or Milestone",
            ),
        ),
        BulkVerb.INTERRUPT: (
            ("asks each Run's driver to interrupt its current turn",),
            (
                "does not end any Run",
                "does not release the Runs' Tasks, which keep their status and lease",
            ),
        ),
        BulkVerb.RETRY: (
            ("asks each Run's driver to retry it",),
            (
                "does not rewrite any Run's recorded outcome",
                "does not release the Runs' Tasks, which keep their status and lease",
            ),
        ),
        BulkVerb.RELEASE: (
            (
                "moves each CLAIMED Task back to PLANNED and releases its lease",
                "files a release record naming the cause and the actor on each Task",
            ),
            (
                "does not cancel, stop or retry any Run; a Task a Run is open on is rejected",
                "does not change any Task's Batch or criteria, or move any Batch",
                "releases only a lease the actor holds; another principal's claim is rejected",
            ),
        ),
    }
)

#: What happens to an item whose record moved between confirmation and
#: opening. Stated on every confirmation, because it is the rule that
#: decides whether a stale selection can hit something unintended.
INVALIDATION_RULE: Final = (
    "each item is anchored at the revision it was confirmed at; an item whose revision "
    "moved is rejected on its own, never re-targeted, and the other items proceed"
)


class BulkItemState(StrEnum):
    """Where one item of an operation stands."""

    REQUESTED = "requested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    INVALIDATED = "invalidated"


#: The moves an item may take, each on an observation. ``unknown`` is
#: reachable from ``requested`` as well as ``accepted`` because an ask
#: that failed in transit leaves even its acceptance unobserved; and a
#: reconcile that finds the ask merely accepted moves ``unknown`` back to
#: ``accepted`` rather than inventing an outcome.
ITEM_EDGES: Final[Mapping[BulkItemState, frozenset[BulkItemState]]] = MappingProxyType(
    {
        BulkItemState.REQUESTED: frozenset(
            {BulkItemState.ACCEPTED, BulkItemState.REJECTED, BulkItemState.UNKNOWN}
        ),
        BulkItemState.ACCEPTED: frozenset(
            {BulkItemState.CONFIRMED, BulkItemState.UNKNOWN, BulkItemState.INVALIDATED}
        ),
        BulkItemState.UNKNOWN: frozenset(
            {BulkItemState.ACCEPTED, BulkItemState.CONFIRMED, BulkItemState.INVALIDATED}
        ),
        BulkItemState.REJECTED: frozenset(),
        BulkItemState.CONFIRMED: frozenset(),
        BulkItemState.INVALIDATED: frozenset(),
    }
)

#: The states no later observation moves an item out of.
SETTLED_ITEM_STATES: Final[frozenset[BulkItemState]] = frozenset(
    state for state, successors in ITEM_EDGES.items() if not successors
)

#: The item state each control disposition is read as. ``idle`` has no
#: row: it is what a request that produced no fact projects to, and an
#: item is only read from the ledger once its ask was answered.
DISPOSITION_STATES: Final[Mapping[ControlDisposition, BulkItemState]] = MappingProxyType(
    {
        ControlDisposition.REQUESTING: BulkItemState.REQUESTED,
        ControlDisposition.ACCEPTED: BulkItemState.ACCEPTED,
        ControlDisposition.REJECTED: BulkItemState.REJECTED,
        ControlDisposition.SUPERSEDED: BulkItemState.REJECTED,
        ControlDisposition.INVALIDATED: BulkItemState.INVALIDATED,
        ControlDisposition.CONFIRMED: BulkItemState.CONFIRMED,
        ControlDisposition.UNKNOWN: BulkItemState.UNKNOWN,
        ControlDisposition.RECOVERY: BulkItemState.UNKNOWN,
    }
)


def advance(prior: BulkItemState, observed: BulkItemState) -> BulkItemState:
    """Return where an item stands once *observed* is read over *prior*.

    A refusal found while reconciling an ``unknown`` item means the state
    it was asked against changed underneath it, so it reads as
    ``invalidated`` rather than as a fresh rejection.

    Args:
        prior: Where the item stood.
        observed: What was just observed of it.

    Returns:
        The item's new state; *prior* when nothing new was observed.

    Raises:
        ValueError: *observed* is not reachable from *prior*, which would
            rewrite an outcome that was already settled or skip one.
    """
    if observed is prior:
        return prior
    if prior is BulkItemState.UNKNOWN and observed is BulkItemState.REJECTED:
        return BulkItemState.INVALIDATED
    if observed not in ITEM_EDGES[prior]:
        raise ValueError(f"a {prior.value} item cannot become {observed.value}")
    return observed


def require_item_kinds(verb: BulkVerb, item_refs: Iterable[QualifiedUrn]) -> None:
    """Refuse an item of a kind *verb* does not take.

    Args:
        verb: The bulk verb.
        item_refs: The items it would name.

    Raises:
        ValueError: An item is not of the kind the verb names.
    """
    kind = BULK_ITEM_KINDS[verb]
    foreign = sorted(str(ref) for ref in item_refs if ref.kind is not kind)
    if foreign:
        raise ValueError(f"{verb.value} takes {kind.value} items, not: {', '.join(foreign)}")


def canonical_items(item_refs: Iterable[QualifiedUrn]) -> tuple[QualifiedUrn, ...]:
    """Return *item_refs* in the one order an operation lists them in.

    The order is canonical rather than the order of selection, so two
    selections of the same items name the same operation.

    Args:
        item_refs: The selected items.

    Returns:
        The items, sorted by URN.

    Raises:
        ValueError: An item is named twice, which would give it two
            results.
    """
    refs = tuple(item_refs)
    repeated = sorted({str(ref) for ref in refs if refs.count(ref) > 1})
    if repeated:
        raise ValueError(f"items are named more than once: {', '.join(repeated)}")
    return tuple(sorted(refs, key=str))


class BulkConfirmation(_FrozenModel):
    """What the operator is shown before a bulk operation opens.

    Attributes:
        verb: The verb every item receives.
        target_count: How many items the operation names.
        item_refs: The items, in canonical order.
        effects: What the verb does to each item.
        non_effects: What it does not do, stated so it is not assumed.
        invalidation_rule: What happens to an item that moved before the
            operation opened.
    """

    verb: BulkVerb
    target_count: StrictPositiveInt
    item_refs: tuple[BulkItemUrn, ...] = Field(min_length=1)
    effects: tuple[NonEmptyStr, ...] = Field(min_length=1)
    non_effects: tuple[NonEmptyStr, ...] = Field(min_length=1)
    invalidation_rule: NonEmptyStr

    @model_validator(mode="after")
    def _counts_its_items(self) -> Self:
        """Refuse a count that disagrees with the items it counts.

        Raises:
            ValueError: The stated count is not the number of items, or an
                item is not of the kind the verb names.
        """
        require_item_kinds(self.verb, self.item_refs)
        if self.target_count != len(self.item_refs):
            raise ValueError(
                f"target_count {self.target_count} does not count {len(self.item_refs)} items"
            )
        return self

    @property
    def digest(self) -> str:
        """Return the digest an operation opened under this confirmation carries."""
        return canonical_digest(self.model_dump(mode="json"))


def confirm_bulk(verb: BulkVerb, item_refs: Iterable[QualifiedUrn]) -> BulkConfirmation:
    """Return the confirmation *verb* over *item_refs* must be opened under.

    Args:
        verb: The bulk verb.
        item_refs: The items it names, in any order.

    Returns:
        The confirmation, naming the items in canonical order.

    Raises:
        ValueError: An item is named twice, no item is named, or an item
            is not of the kind the verb names.
    """
    refs = canonical_items(item_refs)
    effects, non_effects = _CONSEQUENCES[verb]
    return BulkConfirmation(
        verb=verb,
        target_count=len(refs),
        item_refs=refs,
        effects=effects,
        non_effects=non_effects,
        invalidation_rule=INVALIDATION_RULE,
    )


class BulkItemResult(_FrozenModel):
    """One item's own ledger inside an operation.

    Attributes:
        state: Where the item stands.
        code: Why a rejected item was refused; ``None`` otherwise.
        detail: One sentence an operator reads.
        control_request_ref: The Run control the item was asked under, or
            ``None`` when it was rejected before anything was asked or is
            a Task, which is released by a lifecycle move, not a control.
    """

    state: BulkItemState
    code: RefusalCode | None = None
    detail: Annotated[str, StringConstraints(strict=True, max_length=1000)] = ""
    control_request_ref: ControlRequestId | None = None

    @model_validator(mode="after")
    def _rejection_says_why(self) -> Self:
        """Refuse a rejected item that carries no refusal code.

        Raises:
            ValueError: The item is rejected with no code, so nobody can
                tell a denial from a moved revision.
        """
        if self.state is BulkItemState.REJECTED and self.code is None:
            raise ValueError("a rejected item carries the code it was refused with")
        return self


def aggregate_of(results: Iterable[BulkItemResult]) -> dict[BulkItemState, int]:
    """Return how many items stand in each state, every state listed.

    Args:
        results: The per-item results.

    Returns:
        A count for every :class:`BulkItemState`, zero included.
    """
    counts = dict.fromkeys(BulkItemState, 0)
    for result in results:
        counts[result.state] += 1
    return counts


class BulkOperation(_FrozenModel):
    """One operator decision over an explicit set of Runs or Tasks.

    A partial outcome -- some confirmed, some unknown, some rejected -- is
    a normal result, so the record carries counts by state and no single
    success flag.

    Attributes:
        operation_kind: The one subject this operation is.
        verb: The verb every item received.
        item_refs: The items, in canonical order.
        expected_revisions: The revision each item was confirmed at, its
            compare-and-swap anchor, keyed by item URN.
        item_results: Each item's own result, keyed by item URN.
        aggregate: How many items stand in each state.
        idempotency_key: The client's name for the operation.
        actor: Who opened it.
        confirmation_digest: The digest of the confirmation it opened under.
    """

    operation_kind: Literal["bulk_control"] = "bulk_control"
    verb: BulkVerb
    item_refs: tuple[BulkItemUrn, ...] = Field(min_length=1)
    expected_revisions: dict[str, StrictPositiveInt]
    item_results: dict[str, BulkItemResult]
    aggregate: dict[BulkItemState, StrictNonNegativeInt]
    idempotency_key: BulkIdempotencyKey
    actor: PrincipalKey
    confirmation_digest: Sha256DigestStr

    @model_validator(mode="after")
    def _one_result_per_item(self) -> Self:
        """Refuse an operation whose ledgers do not match its item set.

        Raises:
            ValueError: The items are not unique, not canonical or not of
                the verb's kind; an anchor or a result is missing for an
                item or present for something that is not one; or the
                aggregate disagrees with the results it counts.
        """
        require_item_kinds(self.verb, self.item_refs)
        if canonical_items(self.item_refs) != self.item_refs:
            raise ValueError("item_refs must be listed in canonical order")
        items = {str(ref) for ref in self.item_refs}
        for field, keyed in (
            ("expected_revisions", self.expected_revisions),
            ("item_results", self.item_results),
        ):
            if set(keyed) != items:
                raise ValueError(f"{field} must name exactly the operation's items")
        if self.aggregate != aggregate_of(self.item_results.values()):
            raise ValueError("aggregate must count item_results by state")
        return self


__all__ = [
    "BULK_CONTROLS",
    "BULK_ITEM_KINDS",
    "DISPOSITION_STATES",
    "INVALIDATION_RULE",
    "ITEM_EDGES",
    "SETTLED_ITEM_STATES",
    "BulkConfirmation",
    "BulkIdempotencyKey",
    "BulkItemResult",
    "BulkItemState",
    "BulkOperation",
    "BulkVerb",
    "RefusalCode",
    "advance",
    "aggregate_of",
    "canonical_items",
    "confirm_bulk",
    "require_item_kinds",
]
