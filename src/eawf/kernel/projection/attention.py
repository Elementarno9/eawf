"""The attention reducer: whose open items these are, where each lands, and what may toast.

Attention is a projection, never a ledger of its own. Every item is one row of the
Attention register the daemon served, carried with the address and the exact revision of
the record it came from, so an answer given to it names the revision the operator was
shown and a stale one is refused by the record rather than landed on top of it.

Three questions are answered here, once, for every surface that asks them.

Which bucket. Eight exception buckets in a fixed severity-first order partition the
register totally and disjointly: an open item lands in exactly one, and under ``needs
operator`` in exactly one :class:`AttentionNeedKind`. A Run that stopped responding lands
under ``stalled`` for as long as the stall the daemon raised over it stands; a failed Run
that is still its open Task's newest attempt under ``failed``; a Run running unstalled under
``active``; a ceiling breach under ``over budget``; and a blocking audit verdict of a Batch
still open under ``rejected``. ``lost`` has no record of its own, so it is a declared hole
whose zero is the absence of a producer, not a count that was taken.

Whose. An item is *mine* when the console acts as a principal the item is addressed to:
the one principal its record names, or every principal when it names none. A provider
permission names no principal but the classes that may decide it, and a principal on a
console acts as an operator, so it is addressed to every principal only when the operator
class may approve or deny it. A console
acting as nobody has no ``mine`` to count, so the count comes back unknown naming that,
rather than borrowing the all-principals count. A notice never counts: it blocks nothing.

What may interrupt. :class:`NotificationClass` is closed over five classes, and
:data:`NOTIFICATION_MATRIX` states for each whether it may raise a toast and what decided
that. A toast is the only interruption any class may make; nothing here can reach a modal,
a focus change or a route change. Delivery is at most once per principal per revision,
and a revision already on the register when a console first read it counts as delivered,
so reconnecting or restarting never replays a storm of toasts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from pydantic import ConfigDict

from eawf.kernel.projection.compute import (
    RUN_STATE_KIND,
    SNOOZED_FACT,
    STALL_KIND,
    ProjectionRow,
)
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    RegisterView,
    count_field,
    revision_of,
)
from eawf.kernel.projection.truth import (
    TruthField,
    TruthState,
)
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.base import Epoch2Model
from eawf.kernel.state.epoch2.pending_action import PendingActionStatus
from eawf.kernel.state.epoch2.run import RunStatus, SuspensionReason
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


class AttentionBucket(StrEnum):
    """The eight exception buckets, in the severity-first order every surface draws."""

    FAILED = "failed"
    LOST = "lost"
    NEEDS_OPERATOR = "needs operator"
    STALLED = "stalled"
    OVER_BUDGET = "over budget"
    REJECTED = "rejected"
    ACTIVE = "active"
    QUEUED = "queued"


class AttentionNeedKind(StrEnum):
    """What an item under ``needs operator`` needs: a permission, an answer, or readiness."""

    PERMISSION = "permission"
    ANSWER = "answer"
    READINESS = "readiness"


class NotificationClass(StrEnum):
    """The closed set of things that may be announced to an operator."""

    NEEDS_PERMISSION = "needs_permission"
    NEEDS_ANSWER = "needs_answer"
    STOPPED_RESPONDING = "stopped_responding"
    RUN_FINISHED = "run_finished"
    BUDGET_PASSED = "budget_passed"


class ToastPolicy(StrEnum):
    """Whether a class may raise a toast, which is all ``may interrupt`` ever means."""

    YES = "yes"
    NO = "no"
    ONCE_PER_REVISION = "once per revision"


class BucketSource(StrEnum):
    """How a bucket's count is known.

    ``DERIVED`` buckets are counted off the register's rows. ``HOLE`` buckets have no
    producer at all; their zero is declared, never counted.
    """

    DERIVED = "derived"
    HOLE = "hole"


#: Why ``lost`` has no record of its own: a Run that stops answering is the stall the
#: daemon raises over it, and that lands under ``stalled``.
LOST_HOLE_REASON: Final = (
    "nothing records a Run as lost: a Run that stopped answering is the stall the daemon "
    "raised over it, counted under stalled"
)

#: Why each bucket with no producer states no count.
_BUCKET_REASONS: Final[Mapping[AttentionBucket, str]] = MappingProxyType(
    {AttentionBucket.LOST: LOST_HOLE_REASON}
)

#: How each bucket's count is known. Total over :class:`AttentionBucket`, checked below.
BUCKET_SOURCES: Final[Mapping[AttentionBucket, BucketSource]] = MappingProxyType(
    {
        AttentionBucket.FAILED: BucketSource.DERIVED,
        AttentionBucket.LOST: BucketSource.HOLE,
        AttentionBucket.NEEDS_OPERATOR: BucketSource.DERIVED,
        AttentionBucket.STALLED: BucketSource.DERIVED,
        AttentionBucket.OVER_BUDGET: BucketSource.DERIVED,
        AttentionBucket.REJECTED: BucketSource.DERIVED,
        AttentionBucket.ACTIVE: BucketSource.DERIVED,
        AttentionBucket.QUEUED: BucketSource.DERIVED,
    }
)

#: Why the console's ``mine`` count is unknown when it acts as nobody.
NO_PRINCIPAL_MINE_REASON: Final = "this console acts as no principal, so nothing here is yours"

#: The pending-action statuses that are open and in front of the operator.
_ASKED: Final = frozenset({PendingActionStatus.WAITING.value})

#: The pending-action statuses that are open and not yet in front of anyone.
_NOT_YET_ASKED: Final = frozenset({PendingActionStatus.CREATED.value})

#: The status the register states an unresolved provider permission under.
_PERMISSION_OPEN: Final = "open"

#: The statuses of a question still waiting on an answer.
_QUESTION_LIVE: Final = frozenset({OpenQuestionStatus.OPEN.value, OpenQuestionStatus.BLOCKED.value})

#: The principal class a named principal acts as on a console: a person answering.
CONSOLE_PRINCIPAL_CLASS: Final = "operator"


class NotificationPolicy(Epoch2Model):
    """One row of the presentation matrix: a class, whether it may toast, who decided.

    Attributes:
        notification_class: The class the row states.
        may_interrupt: Whether it may raise a toast, the one occlusion the console allows.
        decided_by: The contract that decided it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    notification_class: NotificationClass
    may_interrupt: ToastPolicy
    decided_by: str


class NotificationMatrixView(Epoch2Model):
    """The presentation matrix the notifications route projects read-only.

    Attributes:
        classes: One row per notification class, in the order the matrix states them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    classes: tuple[NotificationPolicy, ...]


#: The presentation matrix. A terminal transition is an Activity row and never a toast;
#: a budget crossing toasts once per revision, at the notify fraction with its grace met
#: and at the limit band, and never at any other fraction.
NOTIFICATION_MATRIX: Final[NotificationMatrixView] = NotificationMatrixView(
    classes=(
        NotificationPolicy(
            notification_class=NotificationClass.NEEDS_PERMISSION,
            may_interrupt=ToastPolicy.YES,
            decided_by="attention projection",
        ),
        NotificationPolicy(
            notification_class=NotificationClass.NEEDS_ANSWER,
            may_interrupt=ToastPolicy.YES,
            decided_by="attention projection",
        ),
        NotificationPolicy(
            notification_class=NotificationClass.STOPPED_RESPONDING,
            may_interrupt=ToastPolicy.YES,
            decided_by="attention projection",
        ),
        NotificationPolicy(
            notification_class=NotificationClass.RUN_FINISHED,
            may_interrupt=ToastPolicy.NO,
            decided_by="run lifecycle",
        ),
        NotificationPolicy(
            notification_class=NotificationClass.BUDGET_PASSED,
            may_interrupt=ToastPolicy.ONCE_PER_REVISION,
            decided_by="budget notify fraction",
        ),
    )
)

#: Which source each class is announced for. The three suspension classes are keyed by
#: the need an attention item states and by the suspension that produces it; the other
#: two are a Run's terminal move and a budget notice, neither of them an item.
CLASS_SOURCES: Final[Mapping[NotificationClass, tuple[AttentionNeedKind | None, str]]] = (
    MappingProxyType(
        {
            NotificationClass.NEEDS_PERMISSION: (
                AttentionNeedKind.PERMISSION,
                SuspensionReason.AWAITING_PERMISSION_GRANT.value,
            ),
            NotificationClass.NEEDS_ANSWER: (
                AttentionNeedKind.ANSWER,
                SuspensionReason.AWAITING_OPERATOR_INPUT.value,
            ),
            NotificationClass.STOPPED_RESPONDING: (None, AttentionBucket.STALLED.value),
            NotificationClass.RUN_FINISHED: (None, "run terminal transition"),
            NotificationClass.BUDGET_PASSED: (None, "budget notice"),
        }
    )
)


def _check_tables() -> None:
    """Refuse a table that is not total over its enum.

    Raises:
        ValueError: A bucket has no source, a class has no matrix row or no source, or
            the matrix states a class twice. Raised at import, because a surface drawing
            from a partial table would render a silent gap.
    """
    defects: list[str] = []
    defects += [
        f"bucket {b.value!r} has no source" for b in AttentionBucket if b not in BUCKET_SOURCES
    ]
    stated = [row.notification_class for row in NOTIFICATION_MATRIX.classes]
    if sorted(stated) != sorted(NotificationClass):
        defects.append("the notification matrix does not state every class exactly once")
    defects += [
        f"class {c.value!r} has no source" for c in NotificationClass if c not in CLASS_SOURCES
    ]
    if defects:
        raise ValueError(f"attention tables are invalid: {'; '.join(defects)}")


_check_tables()


def toast_policy(notification_class: NotificationClass) -> ToastPolicy:
    """Return whether ``notification_class`` may raise a toast."""
    for row in NOTIFICATION_MATRIX.classes:
        if row.notification_class is notification_class:
            return row.may_interrupt
    raise KeyError(notification_class)


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionItem:
    """One open attention item, bound to the record and revision it was read from.

    Attributes:
        key: The source record's public key, which a selection restores by.
        source_ref: The source record's canonical address.
        revision: The source record's exact revision, which an answer is addressed to.
        bucket: The exception bucket the item lands in.
        need: The ``needs operator`` sub-bucket; ``None`` in every other bucket.
        assignee_ref: The one principal the item is addressed to; ``None`` addresses it
            to every principal.
        notification_class: What the item is announced as.
        deciding_classes: The principal classes that may decide the item, for a record
            that names classes rather than a principal; ``None`` when it names none.
        read_only: Whether the item is a notice: listed for its audience, but nothing
            answers it, so it is never counted, jumped to, toasted or offered a verb.
        snoozed_by: The principals whose own snooze of the item still stands when the
            register was read; it is out of their count and nobody else's.
    """

    key: str
    source_ref: str
    revision: int
    bucket: AttentionBucket
    need: AttentionNeedKind | None
    assignee_ref: str | None
    notification_class: NotificationClass
    deciding_classes: frozenset[str] | None = None
    read_only: bool = False
    snoozed_by: frozenset[str] = frozenset()

    def addressed_to(self, principal: str) -> bool:
        """Return whether ``principal`` is in this item's audience."""
        if self.deciding_classes is not None:
            return CONSOLE_PRINCIPAL_CLASS in self.deciding_classes
        return self.assignee_ref is None or self.assignee_ref == principal


@dataclass(frozen=True, slots=True, kw_only=True)
class BucketCount:
    """One bucket's count, or why it has none.

    Attributes:
        bucket: The bucket counted.
        need: The ``needs operator`` sub-bucket this row counts; ``None`` for a top level.
        count: The open items in it.
        source: How the count is known.
        reason: Why a hole's zero is not a count; ``None`` for a counted bucket.
    """

    bucket: AttentionBucket
    need: AttentionNeedKind | None
    count: int
    source: BucketSource
    reason: str | None = None

    @property
    def label(self) -> str:
        """Return the label a strip or rail draws: the need under its bucket."""
        return f"↳ {self.need.value}" if self.need is not None else self.bucket.value


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionView:
    """The Attention register reduced to its open items, at one cursor.

    Attributes:
        scope_id: The scope the register was read for.
        source_cursor: The committed ``canonical_sequence`` it was read through.
        items: Every open item, severity-first, then by key.
    """

    scope_id: str
    source_cursor: str
    items: tuple[AttentionItem, ...]

    def open_for(self, principal: str) -> tuple[AttentionItem, ...]:
        """Return the open items ``principal`` is in the audience of and has not snoozed.

        Notices are left out; an item this principal snoozed is out of their count alone.
        """
        return tuple(
            i
            for i in self.blocking()
            if i.addressed_to(principal) and principal not in i.snoozed_by
        )

    def blocking(self) -> tuple[AttentionItem, ...]:
        """Return every open item, whoever it is addressed to, notices left out."""
        return tuple(i for i in self.items if not i.read_only)

    def bucket_counts(self) -> tuple[BucketCount, ...]:
        """Return every bucket in order, the ``needs operator`` needs after their parent.

        The parent's count is its children's sum, so the strip, the rail and the summary
        line cannot disagree about it.
        """
        out: list[BucketCount] = []
        for bucket in AttentionBucket:
            source = BUCKET_SOURCES[bucket]
            reason = _BUCKET_REASONS.get(bucket)
            count = sum(1 for i in self.items if i.bucket is bucket)
            out.append(
                BucketCount(bucket=bucket, need=None, count=count, source=source, reason=reason)
            )
            if bucket is AttentionBucket.NEEDS_OPERATOR:
                out.extend(
                    BucketCount(
                        bucket=bucket,
                        need=need,
                        count=sum(1 for i in self.items if i.need is need),
                        source=source,
                    )
                    for need in AttentionNeedKind
                )
        return tuple(out)


def _snoozed_by(row: ProjectionRow, read_at: datetime | None) -> frozenset[str]:
    """Return the principals whose snooze of ``row`` still stands at ``read_at``.

    A register that states no read instant cannot say a snooze lapsed, so every stated
    snooze stands; a deadline that does not parse hides nothing.
    """
    held: set[str] = set()
    for name, value in row.facts.items():
        if not name.startswith(SNOOZED_FACT):
            continue
        try:
            until = datetime.fromisoformat(value)
        except ValueError:
            continue
        if read_at is None or until > read_at:
            held.add(name.removeprefix(SNOOZED_FACT))
    return frozenset(held)


def _item(row: ProjectionRow, read_at: datetime | None) -> AttentionItem | None:
    """Return the open item one register row is, or ``None`` when it is closed or unplaced."""
    status = row.status.value if row.status.state is TruthState.KNOWN else None
    if status in _ASKED:
        bucket, need = AttentionBucket.NEEDS_OPERATOR, AttentionNeedKind.ANSWER
    elif status in _NOT_YET_ASKED:
        bucket, need = AttentionBucket.QUEUED, None
    else:
        return None
    return AttentionItem(
        key=row.key,
        source_ref=row.urn,
        revision=row.revision,
        bucket=bucket,
        need=need,
        assignee_ref=row.assignee_ref,
        notification_class=NotificationClass.NEEDS_ANSWER,
        snoozed_by=_snoozed_by(row, read_at),
    )


def _permission_item(row: ProjectionRow) -> AttentionItem | None:
    """Return the open item a provider permission row is, or ``None`` once it is resolved."""
    if row.status.state is not TruthState.KNOWN or row.status.value != _PERMISSION_OPEN:
        return None
    deciding = {
        name.strip()
        for verb in ("approve", "deny")
        for name in row.facts.get(verb, "").split(",")
        if name.strip()
    }
    return AttentionItem(
        key=row.key,
        source_ref=row.urn,
        revision=row.revision,
        bucket=AttentionBucket.NEEDS_OPERATOR,
        need=AttentionNeedKind.PERMISSION,
        assignee_ref=None,
        notification_class=NotificationClass.NEEDS_PERMISSION,
        deciding_classes=frozenset(deciding),
    )


def _question_item(row: ProjectionRow) -> AttentionItem | None:
    """Return the open item a question row is, or ``None`` once it is resolved.

    A question names no principal it is addressed to, so it is every principal's.
    """
    if row.status.state is not TruthState.KNOWN or row.status.value not in _QUESTION_LIVE:
        return None
    return AttentionItem(
        key=row.key,
        source_ref=row.urn,
        revision=row.revision,
        bucket=AttentionBucket.NEEDS_OPERATOR,
        need=AttentionNeedKind.ANSWER,
        assignee_ref=None,
        notification_class=NotificationClass.NEEDS_ANSWER,
    )


def _run_item(row: ProjectionRow) -> AttentionItem:
    """Return the item a run-ledger row is: a stalled, failed or running Run, or a breach.

    A Run carries no owner of its own, so each is addressed to every principal. A stall is
    a Run that stopped responding, which a principal resumes or lets go, so it is counted
    and announced. The only admission past a ceiling is the adoption of a subagent the
    host already spawned, whose tree belongs to the operator running that host, so a
    breach is only listed. A failed Run is answered by its next attempt and a running one
    by nothing, so both are listed and answered by no verb here.
    """
    kind = row.facts.get("kind")
    status = row.status.value if row.status.state is TruthState.KNOWN else None
    if kind == STALL_KIND:
        bucket, announced = AttentionBucket.STALLED, NotificationClass.STOPPED_RESPONDING
    elif kind == RUN_STATE_KIND:
        failed = status == RunStatus.FAILED.value
        bucket = AttentionBucket.FAILED if failed else AttentionBucket.ACTIVE
        announced = NotificationClass.RUN_FINISHED
    else:
        bucket, announced = AttentionBucket.OVER_BUDGET, NotificationClass.BUDGET_PASSED
    return AttentionItem(
        key=row.key,
        source_ref=row.urn,
        revision=row.revision,
        bucket=bucket,
        need=None,
        assignee_ref=None,
        notification_class=announced,
        read_only=bucket is not AttentionBucket.STALLED,
    )


def _verdict_item(row: ProjectionRow) -> AttentionItem:
    """Return the item a blocking audit verdict is: its Batch waits on a repair.

    The repair is the Batch's own next cycle, so the verdict is listed and answered by no
    verb here.
    """
    return AttentionItem(
        key=row.key,
        source_ref=row.urn,
        revision=row.revision,
        bucket=AttentionBucket.REJECTED,
        need=None,
        assignee_ref=None,
        notification_class=NotificationClass.RUN_FINISHED,
        read_only=True,
    )


def build_attention_view(register: RegisterView) -> AttentionView:
    """Return the Attention register reduced to its open items.

    Args:
        register: The Attention route's read model.

    Returns:
        The open items, severity-first.

    Raises:
        ValueError: ``register`` is another route's read model, whose rows are not items.
    """
    if register.route != ATTENTION_ROUTE:
        raise ValueError(
            f"route {register.route!r} states no attention items; the items are the "
            f"{ATTENTION_ROUTE!r} route's register"
        )
    order = {bucket: index for index, bucket in enumerate(AttentionBucket)}
    items: list[AttentionItem] = []
    for row in register.rows:
        if row.collection is Epoch2Collection.PERMISSION:
            item = _permission_item(row)
        elif row.collection is Epoch2Collection.PENDING_ACTION:
            item = _item(row, register.generated_at)
        elif row.collection is Epoch2Collection.OPEN_QUESTION:
            item = _question_item(row)
        elif row.collection is Epoch2Collection.RUN:
            item = _run_item(row)
        elif row.collection is Epoch2Collection.BATCH:
            item = _verdict_item(row)
        else:
            continue
        if item is not None:
            items.append(item)
    items.sort(key=lambda i: (order[i.bucket], i.key))
    return AttentionView(
        scope_id=register.scope_id,
        source_cursor=register.source_cursor,
        items=tuple(items),
    )


def attention_mine(register: RegisterView, *, principal: str | None) -> TruthField[str]:
    """Return this principal's own open-item count, the one the header's ``!N`` prints.

    Args:
        register: The Attention route's read model.
        principal: Who the console acts as; ``None`` when it acts as nobody.

    Returns:
        The count of open items addressed to ``principal``; unknown naming why
        when the console acts as nobody, rather than the all-principals count.

    Raises:
        ValueError: ``register`` is another route's read model.
    """
    view = build_attention_view(register)
    revision = revision_of(register)
    if principal is None:
        return count_field(
            value=None, revision=revision, refs=(view.scope_id,), reason=NO_PRINCIPAL_MINE_REASON
        )
    mine = view.open_for(principal)
    return count_field(
        value=len(mine),
        revision=revision,
        refs=tuple(i.source_ref for i in mine) or (view.scope_id,),
        reason=None,
    )


def attention_all(register: RegisterView) -> TruthField[str]:
    """Return the open item count across every principal.

    Raises:
        ValueError: ``register`` is another route's read model.
    """
    view = build_attention_view(register)
    blocking = view.blocking()
    return count_field(
        value=len(blocking),
        revision=revision_of(register),
        refs=tuple(i.source_ref for i in blocking) or (view.scope_id,),
        reason=None,
    )


def top_item(register: RegisterView, *, principal: str | None) -> AttentionItem | None:
    """Return the item ``!`` jumps to: this principal's first, else the first of anyone's.

    Raises:
        ValueError: ``register`` is another route's read model.
    """
    view = build_attention_view(register)
    mine = view.open_for(principal) if principal is not None else ()
    candidates = mine or view.blocking()
    return candidates[0] if candidates else None


def delivered_revisions(register: RegisterView) -> frozenset[tuple[str, int]]:
    """Return every item revision on the register now, as already delivered.

    A console seeds its delivery record from its first read, so an item that was open
    before it attached, or before a restart, is never toasted again.

    Raises:
        ValueError: ``register`` is another route's read model.
    """
    return frozenset((i.source_ref, i.revision) for i in build_attention_view(register).items)


def deliveries(
    register: RegisterView,
    *,
    principal: str | None,
    delivered: Iterable[tuple[str, int]],
) -> tuple[AttentionItem, ...]:
    """Return the items to toast to ``principal`` now: each revision at most once.

    Args:
        register: The Attention register after the change.
        principal: Who the console acts as; a console acting as nobody is delivered
            nothing, because no item is addressed to nobody.
        delivered: The item revisions already delivered to this principal.

    Returns:
        The open items addressed to ``principal`` whose class may toast and whose
        revision has not been delivered, in severity order.

    Raises:
        ValueError: ``register`` is another route's read model.
    """
    if principal is None:
        return ()
    seen = frozenset(delivered)
    return tuple(
        item
        for item in build_attention_view(register).open_for(principal)
        if toast_policy(item.notification_class) is not ToastPolicy.NO
        and (item.source_ref, item.revision) not in seen
    )


__all__ = [
    "BUCKET_SOURCES",
    "CLASS_SOURCES",
    "CONSOLE_PRINCIPAL_CLASS",
    "LOST_HOLE_REASON",
    "NOTIFICATION_MATRIX",
    "NO_PRINCIPAL_MINE_REASON",
    "AttentionBucket",
    "AttentionItem",
    "AttentionNeedKind",
    "AttentionView",
    "BucketCount",
    "BucketSource",
    "NotificationClass",
    "NotificationMatrixView",
    "NotificationPolicy",
    "ToastPolicy",
    "attention_all",
    "attention_mine",
    "build_attention_view",
    "delivered_revisions",
    "deliveries",
    "toast_policy",
    "top_item",
]
