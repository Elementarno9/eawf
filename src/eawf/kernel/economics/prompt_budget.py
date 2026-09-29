"""The prompt budget: one ceiling over the input window every consumer shares.

Three budgets with three owners -- steering, memory injection, and the
per-Run task packet and capsule -- consume one physical resource, the
model's input window for a turn. Sized independently, three healthy
budgets compose into one exhausted window, so the ceiling belongs to the
window: :class:`PromptBudgetPolicy` refuses any set of allocations whose
sum exceeds the usable window, and no consumer defines a ceiling of its
own.

A class is judged on the size its consumer measured, never on an
estimate. A class whose consumer measures nothing is ``unavailable``: it
is reported as such and cannot be enforced, because a ceiling tested
against a guess is a guess. Exhaustion is a named outcome -- a class is
dropped whole or the dispatch is denied -- and never a truncation that
leaves the reader to discover what went missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.runtime.provider import Digest, RuntimeRecord


class BudgetClassId(StrEnum):
    """The closed set of consumers that share the input window."""

    STEERING_ZONE1 = "steering_zone1"
    STEERING_ZONE2 = "steering_zone2"
    MEMORY_INJECTION = "memory_injection"
    TASK_PACKET = "task_packet"
    AUTHORITY_CAPSULE = "authority_capsule"
    TOOL_CATALOG = "tool_catalog"


#: The classes no exhaustion mode may drop, at any priority. The capsule
#: carries the Run's authority; the task packet carries its criteria, the
#: decision receipts it was dispatched under and the exact base it works
#: from. Dropping either would start a Run that no longer knows what it may
#: do or what it is judged against.
UNDROPPABLE_CLASSES: Final[frozenset[BudgetClassId]] = frozenset(
    {BudgetClassId.AUTHORITY_CAPSULE, BudgetClassId.TASK_PACKET}
)

#: A budget policy's stable identity; a revision keeps it.
BudgetPolicyId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9-]{1,63}$")]

#: What happens when a class exceeds its ceiling. Silent truncation is not
#: a value.
ExhaustionMode = Literal["deny_dispatch", "degrade_by_priority"]


class BudgetAllocation(RuntimeRecord):
    """One consumer's hard ceiling inside the usable window.

    Attributes:
        class_id: The consumer the ceiling binds.
        max_tokens: The token ceiling.
        max_bytes: A second ceiling in raw bytes, for a consumer a host
            measures in bytes. Both bind where both are set, and neither
            is derived from the other: no token estimator is stable enough
            to convert one into the other as a gate.
        priority: Drop order under ``degrade_by_priority``, lowest first.
        measured: Whether the consumer reports a measured size. An
            unmeasured class is never enforced.
    """

    class_id: BudgetClassId
    max_tokens: Annotated[StrictInt, Field(ge=1)]
    max_bytes: Annotated[StrictInt, Field(ge=1)] | None = None
    priority: Annotated[StrictInt, Field(ge=0, le=100)]
    measured: StrictBool


class PromptBudgetPolicy(RuntimeRecord):
    """The one ceiling every prompt-budget class is sized against.

    Attributes:
        policy_id: The policy's stable identity.
        revision: Bumped on every change; a revision is a new policy.
        input_window_tokens: The certified window of the route's model.
        reserved_output_tokens: Held back for the response; never
            allocatable.
        allocations: One ceiling per budget class, covering every class.
        on_exhaustion: Whether an exhausted class denies the dispatch or
            is dropped whole in priority order.
    """

    policy_id: BudgetPolicyId
    revision: Annotated[StrictInt, Field(ge=1)]
    input_window_tokens: Annotated[StrictInt, Field(ge=1)]
    reserved_output_tokens: Annotated[StrictInt, Field(ge=1)]
    allocations: Annotated[tuple[BudgetAllocation, ...], Field(min_length=1)]
    on_exhaustion: ExhaustionMode

    @property
    def usable_window_tokens(self) -> int:
        """The window left once the response's reservation is held back."""
        return self.input_window_tokens - self.reserved_output_tokens

    @property
    def policy_digest(self) -> Digest:
        """The digest a dispatch binds, so a later revision is detectable."""
        return canonical_digest(self.model_dump(mode="json"))

    def allocation(self, class_id: BudgetClassId) -> BudgetAllocation:
        """Return the ceiling of *class_id*; the allocations are total."""
        return next(row for row in self.allocations if row.class_id is class_id)

    @model_validator(mode="after")
    def _allocations_fit_the_window(self) -> Self:
        """Refuse an over-committed or partial set of allocations.

        Raises:
            ValueError: The reservation leaves no usable window, a class
                has no row or two, the allocations sum past the usable
                window, or the authority capsule does not carry the
                highest priority.
        """
        if self.usable_window_tokens < 1:
            raise ValueError(
                f"reserved_output_tokens {self.reserved_output_tokens} leaves no usable "
                f"window of input_window_tokens {self.input_window_tokens}"
            )
        classes = [row.class_id for row in self.allocations]
        repeated = sorted({item.value for item in classes if classes.count(item) > 1})
        if repeated:
            raise ValueError(f"budget classes allocated twice: {', '.join(repeated)}")
        missing = sorted(item.value for item in BudgetClassId if item not in classes)
        if missing:
            raise ValueError(f"budget classes with no allocation: {', '.join(missing)}")
        total = sum(row.max_tokens for row in self.allocations)
        if total > self.usable_window_tokens:
            raise ValueError(
                f"allocations sum to {total} tokens, over the usable window of "
                f"{self.usable_window_tokens}"
            )
        capsule = self.allocation(BudgetClassId.AUTHORITY_CAPSULE).priority
        if capsule < max(row.priority for row in self.allocations):
            raise ValueError("authority_capsule must carry the highest priority")
        return self


class RenderedSize(RuntimeRecord):
    """The size one consumer measured for what it actually rendered.

    Attributes:
        class_id: The consumer that measured it.
        tokens: The measured token count, or ``None`` when the consumer
            has no measurement in tokens.
        bytes: The measured byte count, or ``None`` when unmeasured.
    """

    class_id: BudgetClassId
    tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    bytes: Annotated[StrictInt, Field(ge=0)] | None = None


class ClassStatus(StrEnum):
    """Where one class landed against its ceiling."""

    WITHIN = "within"
    EXHAUSTED = "exhausted"
    DROPPED = "dropped"
    UNAVAILABLE = "unavailable"


class ClassVerdict(RuntimeRecord):
    """One class's ceiling beside what its consumer measured."""

    class_id: BudgetClassId
    status: ClassStatus
    ceiling_tokens: StrictInt
    ceiling_bytes: StrictInt | None
    measured_tokens: StrictInt | None
    measured_bytes: StrictInt | None


class PromptBudgetOutcome(RuntimeRecord):
    """What the prompt budget decided for one dispatch, class by class.

    Attributes:
        policy_digest: The policy the outcome was decided under.
        policy_revision: That policy's revision.
        admitted: Whether the dispatch may proceed. It may not while any
            class stays exhausted.
        verdicts: One verdict per budget class.
        exhausted: The classes still over their ceiling, by name.
        dropped: The classes dropped whole, lowest priority first.
    """

    policy_digest: Digest
    policy_revision: StrictInt
    admitted: StrictBool
    verdicts: tuple[ClassVerdict, ...]
    exhausted: tuple[BudgetClassId, ...] = ()
    dropped: tuple[BudgetClassId, ...] = ()

    @model_validator(mode="after")
    def _outcome_names_its_classes(self) -> Self:
        """Keep the decision, the named classes and the verdicts in step.

        Raises:
            ValueError: An undroppable class was dropped, or the named
                classes disagree with the verdicts or the decision.
        """
        undroppable = sorted(item.value for item in self.dropped if item in UNDROPPABLE_CLASSES)
        if undroppable:
            raise ValueError(f"classes that are never droppable were dropped: {undroppable}")
        by_status = {
            status: tuple(row.class_id for row in self.verdicts if row.status is status)
            for status in (ClassStatus.EXHAUSTED, ClassStatus.DROPPED)
        }
        if set(by_status[ClassStatus.EXHAUSTED]) != set(self.exhausted):
            raise ValueError("exhausted must name every exhausted verdict")
        if set(by_status[ClassStatus.DROPPED]) != set(self.dropped):
            raise ValueError("dropped must name every dropped verdict")
        if self.admitted == bool(self.exhausted):
            raise ValueError("a dispatch is admitted exactly when no class stays exhausted")
        return self


def _status(allocation: BudgetAllocation, size: RenderedSize | None) -> ClassStatus:
    """Judge one class on its measured size alone."""
    if not allocation.measured or size is None:
        return ClassStatus.UNAVAILABLE
    over_tokens = size.tokens is not None and size.tokens > allocation.max_tokens
    over_bytes = (
        allocation.max_bytes is not None
        and size.bytes is not None
        and size.bytes > allocation.max_bytes
    )
    if over_tokens or over_bytes:
        return ClassStatus.EXHAUSTED
    bytes_measured = allocation.max_bytes is None or size.bytes is not None
    if size.tokens is not None and bytes_measured:
        return ClassStatus.WITHIN
    return ClassStatus.UNAVAILABLE


def evaluate_prompt_budget(
    policy: PromptBudgetPolicy, sizes: Iterable[RenderedSize]
) -> PromptBudgetOutcome:
    """Judge every class's measured size against the policy's ceilings.

    A class over any ceiling it measured is exhausted. Under
    ``degrade_by_priority`` an exhausted class is dropped whole unless it
    is undroppable; under ``deny_dispatch``, and for an undroppable class
    under either mode, the exhaustion stands and the dispatch is denied.
    A class whose consumer reported nothing, or whose ceiling it did not
    measure, is ``unavailable`` rather than within.

    Args:
        policy: The prompt budget in force.
        sizes: What each consumer measured, at most one per class.

    Returns:
        The outcome, naming every exhausted and every dropped class.

    Raises:
        ValueError: Two sizes name one class.
    """
    reported: dict[BudgetClassId, RenderedSize] = {}
    for size in sizes:
        if size.class_id in reported:
            raise ValueError(f"two sizes reported for {size.class_id.value}")
        reported[size.class_id] = size
    statuses: Mapping[BudgetClassId, ClassStatus] = {
        row.class_id: _status(row, reported.get(row.class_id)) for row in policy.allocations
    }
    droppable = policy.on_exhaustion == "degrade_by_priority"
    dropped = tuple(
        row.class_id
        for row in sorted(policy.allocations, key=lambda item: item.priority)
        if droppable
        and statuses[row.class_id] is ClassStatus.EXHAUSTED
        and row.class_id not in UNDROPPABLE_CLASSES
    )
    verdicts = tuple(
        ClassVerdict(
            class_id=row.class_id,
            status=ClassStatus.DROPPED if row.class_id in dropped else statuses[row.class_id],
            ceiling_tokens=row.max_tokens,
            ceiling_bytes=row.max_bytes,
            measured_tokens=reported[row.class_id].tokens if row.class_id in reported else None,
            measured_bytes=reported[row.class_id].bytes if row.class_id in reported else None,
        )
        for row in policy.allocations
    )
    exhausted = tuple(row.class_id for row in verdicts if row.status is ClassStatus.EXHAUSTED)
    return PromptBudgetOutcome(
        policy_digest=policy.policy_digest,
        policy_revision=policy.revision,
        admitted=not exhausted,
        verdicts=verdicts,
        exhausted=exhausted,
        dropped=dropped,
    )


#: The shipped prompt budget, used where the configuration declares none.
DEFAULT_PROMPT_BUDGET: Final[PromptBudgetPolicy] = PromptBudgetPolicy(
    policy_id="BUD-DEFAULT",
    revision=1,
    input_window_tokens=200_000,
    reserved_output_tokens=32_000,
    on_exhaustion="deny_dispatch",
    allocations=(
        BudgetAllocation(
            class_id=BudgetClassId.AUTHORITY_CAPSULE, max_tokens=4_000, priority=100, measured=True
        ),
        BudgetAllocation(
            class_id=BudgetClassId.TASK_PACKET, max_tokens=24_000, priority=90, measured=True
        ),
        BudgetAllocation(
            class_id=BudgetClassId.STEERING_ZONE1,
            max_tokens=12_000,
            max_bytes=12_288,
            priority=80,
            measured=True,
        ),
        BudgetAllocation(
            class_id=BudgetClassId.TOOL_CATALOG, max_tokens=8_000, priority=60, measured=True
        ),
        BudgetAllocation(
            class_id=BudgetClassId.STEERING_ZONE2, max_tokens=24_000, priority=40, measured=True
        ),
        BudgetAllocation(
            class_id=BudgetClassId.MEMORY_INJECTION, max_tokens=8_000, priority=20, measured=True
        ),
    ),
)


__all__ = [
    "DEFAULT_PROMPT_BUDGET",
    "UNDROPPABLE_CLASSES",
    "BudgetAllocation",
    "BudgetClassId",
    "BudgetPolicyId",
    "ClassStatus",
    "ClassVerdict",
    "ExhaustionMode",
    "PromptBudgetOutcome",
    "PromptBudgetPolicy",
    "RenderedSize",
    "evaluate_prompt_budget",
]
